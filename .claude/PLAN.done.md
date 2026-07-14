# Plan: Day P&L Breakup Modal + prev_close Fix

## Task
Two connected changes:

**1. Day P&L Breakup Modal** — clicking NavStrip P slot 1 opens a modal showing per-account,
per-symbol day P&L breakdown with all formula inputs (prev close, LTP, overnight qty,
buy/sell qty+value, lifetime PnL, settlement PnL, computed day P&L). Zero-day-PnL rows
show ⚠ with tooltip explaining why.

**2. `previous_close` in `daily_book`** — root-cause fix for day P&L = 0 in the
closed-hours snapshot path. Currently `build_snapshot_position_row` sets `close_price = ltp`
(snapshot LTP, same as `last_price`), so `baseDayPnlForPosition` fallback
`pnl − oq × (close − avg)` collapses to 0 for overnight unrealised positions. Fix: store
`previous_close` (yesterday's official settlement price) in `daily_book`, written from
`position.close_price` at the **first snapshot of each trading day** (when Kite's
`close_price` field is still yesterday's settlement, before Kite overwrites it at EOD).
The snapshot reader uses this stored `previous_close` as the PositionRow's `close_price`
so the formula is correct regardless of when the snapshot was captured.

## Agents
- backend: Add `previous_close` column to `daily_book`; write it at first-snapshot-of-day; use it in snapshot reader
- frontend: Build DayPnlBreakup.svelte modal; wire click handler into PositionStrip.svelte
- broker: skip
- doc: skip
- backend-test: Tests for snapshot path fix (previous_close written, used, and day P&L correct)
- playwright: New spec `frontend/tests/day_pnl_breakup.spec.js`

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

---

## Backend agent brief

### Context / root cause

`build_snapshot_position_row` in `backend/api/routes/positions_helpers.py` (line 213-231):
```python
return PositionRow(
    close_price=ltp_f,   # ← snapshot LTP — WRONG, same as last_price
    last_price=ltp_f,
    pnl=total_pnl_f,
    overnight_quantity=qty_i,
    # prev_settlement_pnl never set → None
)
```
`baseDayPnlForPosition` fallback: `total_pnl − qty × (ltp − avg) = 0` (overnight unrealised).

The post-settlement close_price update makes this worse: after Kite overwrites `close_price`
→ today's settlement, any snapshot written at that point also stores `day_pnl = 0`.

**Industry-standard fix**: store `previous_close` (yesterday's official settlement) as a
separate field. It is written **once per trading day** — from `position.close_price` captured
at the first intra-session snapshot, when Kite's field still reflects yesterday's settlement.
It is **frozen** for the rest of the day. Tomorrow's first snapshot writes a new value.

### Step 1 — DB migration

Add column to `daily_book` table:
```sql
ALTER TABLE daily_book ADD COLUMN IF NOT EXISTS previous_close DOUBLE PRECISION;
```

File: add a new migration in `backend/alembic/versions/` (or inline SQL migration per
project pattern — check `docs/MIGRATION.md` and existing migration files to match style).

### Step 2 — Writer (`daily_snapshot.py`)

Find where position rows are written to `daily_book`. Locate the `_positions_rows` function
or equivalent writer.

Add logic: for each (account, symbol) being written, if no `previous_close` has been stored
for **today** yet (i.e., no row with `kind='positions'` and `date(captured_at) = today IST`
exists for this key), write `previous_close = position.close_price` (from the broker row).

If a `previous_close` already exists for today for this key, do NOT overwrite it — preserve
the first-snapshot value.

Check `docs/MIGRATION.md` and the snapshot writer to understand the exact UPSERT pattern
used. Match it exactly.

### Step 3 — Reader (`positions_helpers.py` + `positions.py`)

In `build_snapshot_position_row` (`positions_helpers.py`):
- Accept an optional `previous_close: float | None = None` kwarg
- If `previous_close` is not None and > 0, use it as `close_price` in the PositionRow
- Otherwise fall back to `ltp_f` (current behaviour — safe for rows without the column yet)

In `_positions_snapshot()` (`positions.py`):
- The SQL query already reads `db.ltp, db.day_pnl, db.total_pnl` — extend it to also
  select `db.previous_close`
- Pass `previous_close` through to `build_snapshot_position_row`

This ensures the closed-hours PositionRow has `close_price = yesterday_settlement` (not
snapshot LTP), so `baseDayPnlForPosition` fallback gives the correct non-zero day P&L.

### Step 4 — Verify `_override_stale_close_from_snapshot` still works

`_override_stale_close_from_snapshot` in `positions.py` already overrides `close_price` from
daily_book during LIVE polling. Confirm it is unaffected by this change (it reads `ltp`, not
`previous_close`). No change needed there.

---

## Frontend agent brief

### New file: `frontend/src/lib/DayPnlBreakup.svelte`

**Props**: `{ open: boolean, positions: object[], onClose: () => void }`

**Behaviour:**
- Full-screen backdrop overlay; click backdrop or press Esc to close.
- Header: "Day P&L Breakup" left; total `aggregateDayPnlForPositions(positions)` right,
  coloured pos/neg/flat. Use `fmtMoney` from `$lib/format.js`.
- Import `baseDayPnlForPosition`, `aggregateDayPnlForPositions` from `$lib/data/nav.js`.

**Table (one row per position, sorted by |day_pnl| desc):**

| Column | Value | Source field |
|---|---|---|
| Symbol | `tradingsymbol` | p.tradingsymbol |
| Account | `account` | p.account |
| Prev Close | `close_price` | p.close_price |
| LTP | `last_price` | p.last_price |
| Overnight Qty | `overnight_quantity` | p.overnight_quantity |
| Buy Qty / Val | `day_buy_quantity` @ `day_buy_value` | two sub-values |
| Sell Qty / Val | `day_sell_quantity` @ `day_sell_value` | two sub-values |
| PnL (lifetime) | `pnl` | p.pnl |
| Settle PnL | `prev_settlement_pnl` | p.prev_settlement_pnl (show "—" if null) |
| Day P&L | `baseDayPnlForPosition(p)` | computed |

**Expandable formula row** (click ▸ to toggle per row):
- If `prev_settlement_pnl != null`: `Day P&L = PnL − Settlement = {pnl} − {prevPnl} = {result}`
- Else: `Day P&L = PnL − (oq × (close − avg)) = {pnl} − ({oq} × ({close} − {avg})) = {result}`

**Zero-value warning** (⚠ inline in Day P&L cell when `|baseDayPnlForPosition(p)| < 0.5`):
```js
function _zeroReason(p) {
  const qty   = Number(p?.quantity ?? 0);
  const ltp   = Number(p?.last_price ?? 0);
  const close = Number(p?.close_price ?? 0);
  const oq    = Number(p?.overnight_quantity ?? 0);
  const pnl   = Number(p?.pnl ?? 0);
  const prev  = p?.prev_settlement_pnl;
  if (qty === 0) return 'Flat — closed intraday';
  if (ltp > 0 && close > 0 && Math.abs(ltp - close) < 0.005) return 'LTP equals prev close (stale price)';
  if (prev != null && Math.abs(pnl - Number(prev)) < 0.5) return 'No move from yesterday settlement';
  if (oq === 0 && Math.abs(pnl) < 0.5) return 'Opened today, at cost — no movement yet';
  return 'Day P&L is zero';
}
```

**Account subtotals**: group rows by `account`, show subtotal row between groups.

**Style**: follow `ConfirmModal.svelte` pattern — dark overlay `rgba(0,0,0,0.6)`, `dpb-`
CSS prefix. Tight density. Numeric columns right-aligned. Scrollable tbody, fixed thead.

### Edit: `frontend/src/lib/PositionStrip.svelte`

1. `import DayPnlBreakup from './DayPnlBreakup.svelte';`
2. `let _dayPnlBreakupOpen = $state(false);` (near other `let` declarations)
3. Add `onclick={() => _dayPnlBreakupOpen = true}` + `style="cursor:pointer"` to the
   P slot 1 value span (line ~833: the `dispPositionsToday` span). Keep all existing classes.
4. Render just before closing `</div>` of `.ps-strip`:
   ```svelte
   <DayPnlBreakup
     open={_dayPnlBreakupOpen}
     {positions}
     onClose={() => _dayPnlBreakupOpen = false}
   />
   ```

---

## backend-test agent brief

File: `backend/tests/test_positions_snapshot.py` (new or extend existing snapshot tests)

Tests to add:
1. `previous_close` is written to `daily_book` on first snapshot of the day from
   `position.close_price` (not from `ltp`).
2. Subsequent snapshot writes for the same (account, symbol) today do NOT overwrite
   `previous_close`.
3. `_positions_snapshot()` reads `previous_close` and passes it to `build_snapshot_position_row`.
4. `build_snapshot_position_row` with `previous_close` set: `close_price` in returned
   PositionRow equals the `previous_close` value (not `ltp`).
5. `baseDayPnlForPosition` on that PositionRow returns correct non-zero day P&L for an
   overnight position where `ltp ≠ previous_close`.
6. Post-settlement simulation: `ltp = settlement_price, day_pnl = 0, previous_close = 5400` →
   after fix, `baseDayPnlForPosition` returns `(settlement − 5400) × qty`.
7. Backward-compat: rows without `previous_close` (None) fall back to `ltp_f` as `close_price`
   (no regression for old snapshot rows).

---

## Playwright agent brief

File: `frontend/tests/day_pnl_breakup.spec.js`

Tests (target dev.ramboq.com or localhost):
1. P slot 1 value span is clickable — click opens modal with heading "Day P&L Breakup".
2. Modal table has at least one row with Symbol and Account columns populated.
3. Total value in modal header is a money string (₹ / negative with minus).
4. Any position with Day P&L = 0 has ⚠ icon in that row.
5. Esc key closes modal.
6. Click on backdrop closes modal.
7. Day P&L column cells are right-aligned numerics.
8. Formula expand: clicking ▸ on a row shows the formula breakdown text.

---

## Commit message
feat(pnl): day P&L breakup modal on P slot 1 + previous_close in daily_book fixes snapshot day P&L = 0

## Done when
- `daily_book` has `previous_close` column; writer stores it from `position.close_price` at
  first snapshot of each trading day; subsequent writes freeze it.
- Closed-hours snapshot PositionRows have `close_price = previous_close` (yesterday's
  settlement), making `baseDayPnlForPosition` return correct non-zero day P&L.
- Clicking P slot 1 in NavStrip opens breakup modal.
- Modal shows per-account, per-symbol table: prev close, LTP, overnight qty, buy/sell,
  lifetime PnL, settlement PnL, computed day P&L.
- Zero-day-PnL rows show ⚠ with reason tooltip.
- pytest passes (including new snapshot tests).
- svelte-check 0 errors.
- Playwright spec green on dev.
