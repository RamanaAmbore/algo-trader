# Plan: Delete orphan positions from daily_book after closed-hours refresh

## Task
After market settlement (~16:15 IST), Kite removes closed/squared-off positions from
`broker.positions()`. The current `snapshot_daily_book()` uses an UPSERT that only
inserts or updates rows — it never deletes. Positions no longer returned by the broker
stay in `daily_book` indefinitely. The route's `_positions_snapshot()` has no staleness
filter, so stale closed positions appear in the UI all night.

**Fix:** After each per-account positions UPSERT in `snapshot_daily_book()`, delete
`daily_book` rows for that `(date, account, kind="positions")` whose `symbol` is NOT
in the current broker response. Fail-open: skip the delete if the broker call failed.

---

## How the code is structured

- **`backend/api/algo/daily_snapshot.py`** — contains all snapshot logic
  - `_upsert_rows(rows: list[dict]) -> int` (lines 649–659): upserts a batch, commits immediately
  - `snapshot_daily_book()` (lines 693–783): per-account loop, calls `_fetch_account_data()`,
    builds row batches via `_positions_rows()`, `_holdings_rows()`, then calls `_upsert_rows()`
  - `DailyBook` model: unique constraint on `(date, account, kind, symbol)`
- **`_positions_rows(account, target_date, raw_df, ...)  -> list[dict]`** (lines 509–554):
  returns `[]` when raw_df is None/empty (auth failure or no positions)

---

## Agents

### Agent 1 — backend: daily_snapshot.py
File: `backend/api/algo/daily_snapshot.py`

**Add helper `_delete_orphan_positions(date, account, current_symbols)`** near `_upsert_rows`:

```python
async def _delete_orphan_positions(
    target_date: "date", account: str, current_symbols: set[str]
) -> int:
    """Remove daily_book positions rows whose symbol is no longer in the broker response.

    Only called when the broker successfully returned a positions DataFrame (even if empty).
    An empty set means all positions are closed — that is valid and should clear the table.
    """
    from sqlalchemy import delete as _delete
    async with async_session() as session:
        stmt = _delete(DailyBook).where(
            DailyBook.date == target_date,
            DailyBook.account == account,
            DailyBook.kind == "positions",
            ~DailyBook.symbol.in_(current_symbols) if current_symbols
            else DailyBook.symbol.isnot(None),  # delete all when broker returned nothing
        )
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount
```

**Wire into `snapshot_daily_book()`**: In the per-account loop, after
`await _upsert_rows(p_rows)`, add:

```python
# Delete positions no longer returned by broker (prevents ghost rows post-settlement)
if raw.get("positions") is not None:   # None = broker call failed → fail-open
    _p_symbols = {r["symbol"] for r in p_rows}
    deleted = await _delete_orphan_positions(target_date, account, _p_symbols)
    if deleted:
        logger.info("[SNAPSHOT] pruned %d stale position row(s) for %s %s", deleted, account, target_date)
```

`raw` here is the dict returned by `_fetch_account_data()`. If it returned a DataFrame
(even empty), `raw["positions"]` will be a DataFrame, not None. Check how `_fetch_account_data`
signals failure (None vs exception vs empty DataFrame) and guard accordingly — the goal
is to NOT delete when we don't have a confirmed fresh response from the broker.

---

### Agent 2 — backend-test
File: `backend/tests/test_daily_snapshot.py` (create if not exists) or add to nearest existing test file.

Add 3 tests:
```python
def test_delete_orphan_positions_removes_stale_rows():
    # daily_book has [NIFTY24JUL, BANKNIFTY24JUL, RELIANCE]; broker now returns only [RELIANCE]
    # After _delete_orphan_positions(date, account, {"RELIANCE"}), only RELIANCE remains

def test_delete_orphan_positions_empty_set_clears_all():
    # Broker returned no positions (all closed); current_symbols=set()
    # All positions rows for that (date, account) should be deleted

def test_snapshot_daily_book_prunes_orphans_on_closed_hours_refresh():
    # Mock _fetch_account_data to return positions=[RELIANCE] (NIFTY no longer there)
    # Pre-populate daily_book with both NIFTY and RELIANCE positions rows
    # After snapshot_daily_book(), NIFTY row should be gone, RELIANCE updated
```

---

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(snapshot): delete orphan positions from daily_book after broker refresh — stale closed positions no longer shown off-hours

## Done when
- After `snapshot_daily_book()` runs during off-hours, positions no longer returned by Kite are deleted from daily_book
- `_positions_snapshot()` route returns only currently-open positions
- 3 new tests green, no existing tests broken
- Fail-open: if broker call fails, no deletion occurs
