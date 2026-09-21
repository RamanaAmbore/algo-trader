# Plan: CloseReset settlement_map — raw close_price without backfill/token

## Context

`_build_settlement_map` fetches holdings+positions to get `close_price` per symbol at 8:00 IST.
It calls `_fetch_holdings_direct` / `_fetch_positions_direct` which run the full enrichment
pipeline including `_enrich_holdings` / `_enrich_positions` → `backfill_market_data`.

`backfill_market_data` requires an instrument token to look up live LTP from the mmap tick
buffer. Holdings with no registered token (e.g. HFCL, which hasn't been traded this session
and isn't yet subscribed to KiteTicker) get `prev_close = 0` and are skipped from the
settlement_map. CloseReset doesn't update them → ltp ≠ prev_close → day P&L ≠ 0 at 8:00.

The token is **not needed** for CloseReset. Kite REST already returns the correct `close_price`
(BHAV) in the raw broker response — before enrichment. The enrichment pipeline is wasted work
that corrupts the one column we need.

Fix:
1. Add `raw_only=True` path to `_fetch_holdings_local` and `_fetch_positions_local` that skips
   `_enrich_holdings` / `_enrich_positions` and returns only `[account, tradingsymbol, prev_close]`.
2. Replace `_fetch_holdings_direct` / `_fetch_positions_direct` in `_build_settlement_map` with
   a new lightweight sync helper `_fetch_settlement_closes()` that calls `fetch_holdings(raw_only=True)`
   and `fetch_positions(raw_only=True)`.
3. Remove the holdings-alignment fallback from `fix_daily_book_prev_close` (the `result_h` block
   that set `prev_close = ltp` for holdings not in settlement_map — it was a workaround that is
   now obsolete).
4. Update tests.

## Agents
- backend: skip
- frontend: skip
- broker: In `broker_apis.py`, add `raw_only=False` to `_fetch_holdings_local` and the
  corresponding positions function. In `background.py`, replace the enriched holdings/positions
  fetch in `_build_settlement_map` with the raw path and add `_fetch_settlement_closes()`.
  In `daily_snapshot.py`, remove the holdings-alignment fallback block.
- doc: skip
- backend-test: Update `test_fix_daily_book_prev_close.py` — remove the holdings-alignment
  assertion. Add tests for `raw_only=True` in broker_apis (verify backfill is skipped and
  `prev_close` is the raw broker value).

## Files to change

### 1. `backend/brokers/broker_apis.py`

**`_fetch_holdings_local`** (line ~1387) — add `raw_only: bool = False`:
```python
@for_all_accounts
def _fetch_holdings_local(connections=Connections, account=None, kite=None, broker=None, raw_only=False):
    ...
    df_holdings.rename(columns={'close_price': 'prev_close'}, inplace=True)
    if not df_holdings.empty:
        df_holdings["account"] = account
        df_holdings["type"] = "H"
    _record_fetch(account, ok=True)

    if raw_only:
        cols = [c for c in ["account", "tradingsymbol", "prev_close"] if c in df_holdings.columns]
        return df_holdings[cols]

    df_holdings = _enrich_holdings(df_holdings)
    ...
```

`fetch_holdings(raw_only=True)` already falls through to `_fetch_holdings_local` (non-empty kwargs
→ not the SSOT cache path). No change to `fetch_holdings` needed.

**`_fetch_positions_local`** (line ~2000) — same pattern: add `raw_only=False`; after rename +
account tag, if `raw_only`: return `[account, tradingsymbol, prev_close]` subset; skip
`_enrich_positions`.

`fetch_positions(raw_only=True)` also falls through correctly (non-empty kwargs).

### 2. `backend/api/background.py`

**Add `_fetch_settlement_closes()`** (new sync function, near `_fetch_holdings_direct`):
```python
def _fetch_settlement_closes() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Raw close_price fetch for CloseReset — no enrichment, no backfill, no token needed."""
    try:
        frames_h = broker_apis.fetch_holdings(raw_only=True)
        df_h = pd.concat(frames_h, ignore_index=True) if frames_h else pd.DataFrame()
    except Exception as exc:
        logger.warning("[PREV-CLOSE-FIX] raw holdings close fetch failed: %s", exc)
        df_h = pd.DataFrame()
    try:
        frames_p = broker_apis.fetch_positions(raw_only=True)
        df_p = pd.concat(frames_p, ignore_index=True) if frames_p else pd.DataFrame()
    except Exception as exc:
        logger.warning("[PREV-CLOSE-FIX] raw positions close fetch failed: %s", exc)
        df_p = pd.DataFrame()
    return df_h, df_p
```

**Modify `_build_settlement_map`** — replace the two separate `_run` calls:
```python
# Before:
(df_h, _) = await asyncio.wait_for(_run(_fetch_holdings_direct), timeout=30)
(df_p, _) = await asyncio.wait_for(_run(_fetch_positions_direct), timeout=30)

# After:
(df_h, df_p) = await asyncio.wait_for(_run(_fetch_settlement_closes), timeout=30)
```

### 3. `backend/api/algo/daily_snapshot.py`

**Remove** the holdings-alignment block (the `result_h` block ~line 1037–1053):
```python
# REMOVE this entire block:
result_h = await session.execute(text("""
    UPDATE daily_book
    SET prev_close = ltp
    WHERE date = :today AND kind = 'holdings' ...
"""), {"today": today})
...
```
It was a workaround. The root cause is now fixed.

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

**`test_fix_daily_book_prev_close.py`**: remove the assertion that checks for the
holdings-alignment SQL (`kind = 'holdings'` + `prev_close = ltp`).

**New test** (in `backend/tests/broker/` or `backend/tests/`): verify that
`fetch_holdings(raw_only=True)` returns `prev_close` from the raw broker response
(not zeroed by backfill) and does NOT call `_enrich_holdings`.

## Commit message
refactor(settlement_map): use raw REST close_price for CloseReset — skip backfill, no token needed

## Done when
`_build_settlement_map` gets close_price from raw Kite REST for all holdings+positions
(including HFCL and other no-token symbols). `_enrich_holdings`/`backfill_market_data`
not called during CloseReset. Holdings-alignment fallback in daily_snapshot removed.
pytest green.
