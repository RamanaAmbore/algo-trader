# Plan: fix(movers): supplement NSE EOD snapshot in MCX-open/NSE-closed window

## Context

Gainers/losers shows only NIFTY/BANKNIFTY (underlyings) after NSE close while MCX is still
open (15:30–23:30 IST). Individual stocks are empty during this window.

Root cause: `get_movers()` in `watchlist.py` takes the unified live path when any exchange
is open. In that path, `live_snapshot` only contains fresh MCX quotes (NSE is closed so
MCX keys only). `_combine_movers()` overlays `_session_movers` — but `_session_movers` only
contains symbols that crossed the 1.5% threshold during NSE hours. Only indices typically
cross 1.5%; individual stocks rarely do. So only NIFTY/BANKNIFTY appear.

The NSE EOD snapshot IS written to `movers_snapshots` by `_force_movers_snapshot()` at
15:29 IST (all NSE stocks captured). It's currently only served by `_movers_offhours_response()`
which fires after 23:30 IST when both exchanges are closed.

Fix: when `nse_is_open=False and mcx_is_open=True`, load the NSE EOD snapshot from DB and
merge its rows into `live_snapshot` before `_combine_movers()`. Session stickies still
overlay at highest priority (animation/threshold state preserved).

## Agents

- backend: Implement fix in `backend/api/routes/watchlist.py` (see detail below)
- backend-test: Add test covering the MCX-open/NSE-closed path (see detail below)
- frontend: skip
- broker: skip
- doc: skip
- playwright: skip

## Fix detail

**File:** `backend/api/routes/watchlist.py`

### 1. Add helper `_nse_snapshot_as_live_dict`

Add after `_load_latest_movers_snapshot` (around line 322):

```python
async def _nse_snapshot_as_live_dict(ist_today: str) -> dict[str, dict]:
    """Load the NSE EOD snapshot and return it as a live_snapshot-compatible dict.

    Only returns rows captured today so a stale prior-day snapshot
    doesn't pollute the MCX-open window with yesterday's prices.
    Returns {} on cold DB (no snapshot yet) or date mismatch.
    """
    snap = await _load_latest_movers_snapshot()
    if not snap:
        return {}
    # Guard: only use today's snapshot (captured_at is UTC; compare IST date string)
    from backend.shared.helpers.time_utils import utc_to_ist
    snap_ist_date = utc_to_ist(snap.captured_at).strftime("%Y-%m-%d")
    if snap_ist_date != ist_today:
        return {}
    try:
        rows: list[dict] = json.loads(snap.payload_json)
    except Exception:
        return {}
    return {
        r["symbol"]: r for r in rows
        if isinstance(r, dict) and r.get("exchange") == "NSE"
    }
```

Check the exact time_utils import path and `MoverRow`/snapshot key field names by reading
the file — use `symbol` or whatever field is used as the dict key in the existing
`_movers_offhours_response` deserialization (lines 494-505).

### 2. Merge NSE snapshot into `live_snapshot` in `get_movers()`

In `get_movers()` (~line 2091), after `live_snapshot` is built from MCX live quotes and
before `_combine_movers()` is called, add the NSE snapshot merge:

```python
if not nse_is_open and mcx_is_open:
    _nse_snap = await _nse_snapshot_as_live_dict(ist_today)
    if _nse_snap:
        # MCX live entries take priority over NSE snapshot for same key
        live_snapshot = {**_nse_snap, **live_snapshot}
```

Read the exact location in the file — insert at the point where `live_snapshot` is
fully populated but before the `_combine_movers()` call.

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

### Test detail (backend-test agent)

Add to `backend/tests/test_movers_nse_snapshot.py` (new file):

**`test_nse_snapshot_as_live_dict_returns_nse_rows_for_today`**
- Mock `_load_latest_movers_snapshot()` to return a `MoversSnapshot` with today's IST date
  and `payload_json` containing 2 NSE rows + 1 MCX row
- Assert returned dict has 2 entries, all with `exchange == "NSE"`

**`test_nse_snapshot_as_live_dict_empty_on_stale_date`**
- Mock snapshot with yesterday's `captured_at`
- Assert returned dict is `{}`

**`test_nse_snapshot_as_live_dict_empty_when_no_snapshot`**
- Mock `_load_latest_movers_snapshot()` returning None
- Assert returned dict is `{}`

## Commit message
fix(movers): serve NSE EOD snapshot in MCX-open/NSE-closed window

## Done when
- Individual NSE stocks appear in gainers/losers between 15:30–23:30 IST
- MCX live symbols still appear (not displaced)
- `_session_movers` stickies still overlay at highest priority (unchanged)
- All tests pass, CC gate clean
