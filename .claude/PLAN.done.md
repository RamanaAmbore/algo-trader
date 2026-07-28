# Plan: Expiry close agent — fix P1 defects + P2 gaps + activate auto-close agents

## Task
Audit of the expiry-close system found 2 P1 defects and 2 P2 gaps that prevent reliable
autonomous ITM option closeout on expiry day. Fix all items.

---

## Fixes

### P1-A — Restart blindness (`background.py:1144-1146`)
`_task_expiry_check` computes `check_time` for 09:20 IST. If `now >= check_time`, it adds
`timedelta(days=1)` — so any restart after 09:20 on expiry day silently waits until tomorrow.
No "ran today" tracking exists anywhere (no DB field, `ExpiryState.last_scan` unused).

**Fix:** Add a module-level `_expiry_last_run_date: date | None = None`. In the sleep
calculation, if `now >= check_time` AND `_expiry_last_run_date != today`, skip the sleep
and run immediately (handles the "restarted after 09:20" case). Set
`_expiry_last_run_date = today` after the engine completes. If `_expiry_last_run_date == today`,
add `timedelta(days=1)` as before (already ran today).

```python
_expiry_last_run_date: "date | None" = None  # module-level

# Inside _task_expiry_check loop:
now = timestamp_indian()
today = now.date()
check_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
if now >= check_time:
    if _expiry_last_run_date != today:
        delay_s = 0  # past scheduled time, haven't run yet — fire immediately
    else:
        check_time += timedelta(days=1)
        delay_s = max(0, (check_time - now).total_seconds())
else:
    delay_s = max(0, (check_time - now).total_seconds())
if delay_s:
    await asyncio.sleep(delay_s)
# ... run engine ...
_expiry_last_run_date = today
```

---

### P1-B — Re-scan loop not implemented (`expiry.py:711-758`)
`_rescan_min` is loaded (line 202-204) but never used. `run()` docstring promises periodic
re-scan every 30 min; actual code does one morning scan + one NFO close pass. Options that
cross ITM intraday after 09:20 are never picked up for NFO.

**Fix:** After `_run_nfo_close()` in `run()`, add a timed re-scan loop that runs until
market close (15:25 IST NFO), sleeping `self._rescan_min` minutes between iterations.
Each iteration calls `scan_positions()` fresh, diffs against already-closed symbols, and
calls `_run_nfo_close()` with only the newly-ITM subset.

```python
# After initial _run_nfo_close() call in run():
_closed_syms: set[str] = {p.tradingsymbol for p in state.closed}
_nfo_close_until = now.replace(hour=15, minute=25, second=0, microsecond=0)
while timestamp_indian() < _nfo_close_until:
    await asyncio.sleep(self._rescan_min * 60)
    fresh = self.scan_positions()
    new_itm_nfo = [
        p for p in fresh
        if p.exchange == "NFO"
        and p.moneyness == "ITM"
        and p.tradingsymbol not in _closed_syms
    ]
    if new_itm_nfo:
        logger.info("[EXPIRY] re-scan found %d newly-ITM NFO positions", len(new_itm_nfo))
        await self.close_positions(new_itm_nfo)
        _closed_syms.update(p.tradingsymbol for p in new_itm_nfo)
```

---

### P2-A — Auto-close agents default inactive (`agent_engine.py:969, 999`)
Both `expiry-day-equity-itm-auto-close` (line 969) and `expiry-day-commodity-itm-auto-close`
(line 999) have `status="inactive"`. The T-30min safety net never fires unless manually enabled.

**Fix:** Change both to `status="active"` in the built-in definition. Add a comment
explaining these are safety-net agents that fire only on expiry day when ITM positions exist,
so keeping them active year-round is safe (condition `positions.expiring_today.nfo.is_itm==1`
only evaluates true on expiry day with ITM positions).

---

### P2-B — Index underlying LTP key wrong (`expiry.py:438-442`)
`_fetch_underlying_ltps` constructs `f"NSE:{p.underlying}"` → `"NSE:NIFTY"`. Kite requires
`"NSE:NIFTY 50"` for NIFTY and `"NSE:NIFTY BANK"` for BANKNIFTY. LTP comes back 0,
moneyness returns "UNKNOWN", position skipped.

**Fix:** Add an index key map in expiry.py and use it in `_fetch_underlying_ltps`:

```python
_NSE_INDEX_KEYS = {
    "NIFTY":    "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY":  "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MIDCAP SELECT",
    "SENSEX":    "BSE:SENSEX",
}

# In _fetch_underlying_ltps():
kite_key = _NSE_INDEX_KEYS.get(p.underlying, f"NSE:{p.underlying}")
symbols.add(kite_key)
# Response key is everything after ":" — map back:
ltps = {k.split(":", 1)[-1]: v.get("last_price", 0) for k, v in data.items()}
# Store under plain underlying name (NIFTY, not "NIFTY 50"):
for sym_key, ltp in ltps.items():
    # reverse-map back to underlying name
    rev = {v.split(":",1)[-1]: k for k, v in _NSE_INDEX_KEYS.items()}
    underlying = rev.get(sym_key, sym_key)
    ltp_map[underlying] = ltp
```

---

### P3 — Strengthen test + remove dead docstring claims (`test_expiry_logic.py:66-70`)
Replace the string-presence test with one that actually verifies the re-scan loop fires:

```python
def test_rescan_loop_executes():
    # Mock scan_positions to return ITM position on second call only
    # Verify close_positions called twice (initial + re-scan)
```

---

## Agents

### Agent 1 — backend: expiry.py + background.py
Files: `backend/api/algo/expiry.py`, `backend/api/background.py`

Implement P1-A (restart guard in `_task_expiry_check`), P1-B (re-scan loop in `run()`),
and P2-B (index key map in `_fetch_underlying_ltps`). Read each function in full before
editing. Use `_expiry_last_run_date` as module-level sentinel in background.py.

### Agent 2 — backend: agent_engine.py
File: `backend/api/algo/agent_engine.py`
Change `status="inactive"` → `status="active"` for both `expiry-day-equity-itm-auto-close`
(~line 969) and `expiry-day-commodity-itm-auto-close` (~line 999). Read the full definitions
first.

### Agent 3 — backend-test: expiry tests
File: `backend/tests/test_expiry_logic.py`
- Replace `test_interval_guard_before_scan` (lines 66-70) with a real test that mocks
  `scan_positions` to return an ITM position on second call and verifies the re-scan loop
  calls `close_positions` a second time.
- Add `test_expiry_task_runs_immediately_after_restart`: mock `timestamp_indian()` to return
  a time after 09:20 IST, `_expiry_last_run_date = None` → verify engine runs immediately.
- Add `test_index_ltp_key_maps_nifty`: verify `_fetch_underlying_ltps` constructs
  `"NSE:NIFTY 50"` (not `"NSE:NIFTY"`) for a NIFTY underlying position.

---

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(expiry): restart guard + intraday re-scan loop + activate auto-close agents + fix NIFTY LTP key

## Done when
- Service restart after 09:20 on expiry day → engine runs immediately, not tomorrow
- `_rescan_min` (default 30 min) drives periodic NFO re-scan loop until 15:25 IST
- Both auto-close agents (`expiry-day-equity-itm-auto-close`, `expiry-day-commodity-itm-auto-close`) ship active
- `_fetch_underlying_ltps` sends `NSE:NIFTY 50` (not `NSE:NIFTY`) for index underlyings
- 3+ new tests green, no existing tests broken
