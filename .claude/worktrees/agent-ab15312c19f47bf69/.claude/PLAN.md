# Plan: exchange_clock date-override-first priority

## Task

Fix `exchange_clock.py` so that a date-specific override row has absolute priority over
the default weekday row for the same gate.

Current bug: `is_exchange_open("NSE")` on a holiday still returns True because the code
iterates ALL matching rows and returns True on the first open one — the default row matches
even though an override exists. Same bug in `get_today_gate_sessions`, `is_any_segment_open`,
and `sessions_with_snapshot_time_now`.

**New model — open_time IS the open flag; snapshot_reset_time removed from logic:**

`open_time IS NOT NULL` means open. `open_time IS NULL` means closed. The `is_open` and
`snapshot_reset_time` columns stay in schema but are not used in decision logic.

Columns in use:
- `open_time` — open gate; also the prev-close reset boundary (via default row's open_time)
- `close_time` — close gate
- `snapshot_time` — when EOD snapshot fires

`settlement_cutoff_for(gate)` reads `default_row.open_time` (08:00) — not `snapshot_reset_time`.
Override rows inherit the default row's `open_time` as the reset boundary — a Muhurat evening
session does not change when the next day's prev-close resets.

Two default rows only (no schema change — all columns exist):
```
gate=NON-MCX, date=NULL, weekdays=[0,1,2,3,4], open_time=08:00, close_time=15:30, snapshot_time=15:45
gate=MCX,     date=NULL, weekdays=[0,1,2,3,4], open_time=08:00, close_time=23:30, snapshot_time=23:45
```

Override rows (operator-created, gate + date required, weekdays=NULL):
- Holiday:        `gate=NON-MCX, date=2026-10-02, open_time=NULL, close_time=NULL, snapshot_time=NULL`
- Muhurat:        `gate=NON-MCX, date=2026-11-01, open_time=18:00, close_time=21:00, snapshot_time=21:15`
- Early close:    `gate=NON-MCX, date=2026-12-31, open_time=08:00, close_time=13:00, snapshot_time=13:15`

Interpretation:
- `open_time=NULL` on an override → closed (holiday); default row suppressed
- `open_time=HH:MM` on an override → open with those hours; default row suppressed
- No override for today → apply default row (weekday filter)

The gate name is required because NON-MCX and MCX are independent schedules.

**Override priority rule:**
1. If any row exists for (gate, today's date) → ABSOLUTE precedence; default row suppressed.
   - `open_time=NULL` → closed
   - `open_time=HH:MM` → open with those hours
2. No override → use default row, apply weekday filter.

**`_is_within_session` already works correctly** — it returns False when `open_time is None`.
Only needs the `is_open` boolean check removed from it (rely solely on `open_time` presence).

## Agents

- backend: Fix `exchange_clock.py` only. No other files. No schema change.
  No changes to background.py (it calls exchange_clock public API which will be fixed).

  ### Step 1 — Update `_is_within_session`
  Remove the `if not row.is_open: return False` line. Rely solely on `open_time` presence:
  ```python
  def _is_within_session(row: "ExchangeSchedule") -> bool:
      if row.open_time is None or row.close_time is None:
          return False  # NULL open_time = closed (holiday override or unconfigured)
      now_t = _now_ist().time().replace(second=0, microsecond=0)
      return row.open_time <= now_t < row.close_time
  ```

  ### Step 2 — Add `_effective_gate_rows(gate)`
  ```python
  def _effective_gate_rows(gate: str) -> list["ExchangeSchedule"]:
      """Effective rows for *gate* today — date override takes absolute precedence."""
      upper = gate.upper()
      today = _now_ist().date()
      overrides = [r for r in _CACHE if r.gate.upper() == upper and r.date == today]
      if overrides:
          return overrides  # suppresses default row entirely
      default_rows = [r for r in _CACHE if r.gate.upper() == upper and r.date is None]
      return [r for r in default_rows if _row_matches_now(r)]
  ```

  ### Step 3 — Refactor `is_exchange_open(exchange)`
  ```python
  def is_exchange_open(exchange: str) -> bool:
      if not _CACHE:
          return True
      gate = _exchange_to_gate(exchange)
      if gate is None:
          return True  # Fail-open: unknown exchange.
      return any(_is_within_session(r) for r in _effective_gate_rows(gate))
  ```
  Remove `found_in_cache` tracking — `_exchange_to_gate` returning None handles "not found".

  ### Step 4 — Refactor `get_today_gate_sessions(gate)`
  ```python
  def get_today_gate_sessions(gate: str) -> list["ExchangeSchedule"]:
      return [r for r in _effective_gate_rows(gate) if r.open_time is not None]
  ```

  ### Step 5 — Refactor `is_any_segment_open(exchanges)`
  Collect unique gates from `_CACHE`. Per gate, call `_effective_gate_rows`. Check
  `_is_within_session`. Apply exchange filter if provided. Keep fail-open for empty cache.
  ```python
  def is_any_segment_open(exchanges: list[str] | None = None) -> bool:
      if not _CACHE:
          return True
      upper_set = {e.upper() for e in exchanges} if exchanges else None
      gates = {r.gate for r in _CACHE}
      for gate in gates:
          for row in _effective_gate_rows(gate):
              if upper_set is not None:
                  row_exchs = {e.upper() for e in (row.exchanges or [])}
                  if not row_exchs.intersection(upper_set):
                      continue
              if _is_within_session(row):
                  return True
      return False
  ```

  ### Step 6 — Refactor `sessions_with_snapshot_time_now(tolerance_minutes)`
  Per unique gate, call `_effective_gate_rows`. Skip rows where `open_time is None` (closed —
  holiday override means no snapshot). Check `snapshot_time` within tolerance.
  ```python
  def sessions_with_snapshot_time_now(tolerance_minutes: int = 1) -> list["ExchangeSchedule"]:
      now_t = _now_ist().time().replace(second=0, microsecond=0)
      delta = timedelta(minutes=tolerance_minutes)
      matched = []
      for gate in {r.gate for r in _CACHE}:
          for row in _effective_gate_rows(gate):
              if row.open_time is None:
                  continue  # closed (holiday override) — no snapshot
              if row.snapshot_time is None:
                  continue  # no snapshot configured for this row
              snap_dt = datetime.combine(datetime.today(), row.snapshot_time)
              now_dt  = datetime.combine(datetime.today(), now_t)
              if abs((snap_dt - now_dt).total_seconds()) <= delta.total_seconds():
                  matched.append(row)
      return matched
  ```
  Note: `snapshot_time` must be set explicitly on override rows that want a snapshot
  (e.g. Muhurat row includes `snapshot_time=21:15`). No implicit inheritance — keeps logic simple.

  ### Step 7 — Refactor `settlement_cutoff_for(gate)`
  Use default row's `open_time` as the reset boundary. Remove `snapshot_reset_time` lookup.
  ```python
  async def settlement_cutoff_for(gate: str) -> datetime:
      await refresh()
      reset_time = time(8, 0)  # fallback
      for row in _CACHE:
          if row.gate.upper() == gate.upper() and row.date is None and row.open_time:
              reset_time = row.open_time
              break
      now_ist = _now_ist()
      today_reset = now_ist.replace(
          hour=reset_time.hour, minute=reset_time.minute, second=0, microsecond=0
      )
      if now_ist >= today_reset:
          return today_reset
      return today_reset - timedelta(days=1)
  ```

- frontend: skip
- broker: skip
- doc: skip
- backend-test: Write tests in `backend/tests/test_exchange_clock.py` covering the new
  override-first logic. Build `_CACHE` directly using `types.SimpleNamespace` rows — no DB.

  Each test sets `exchange_clock._CACHE = [...]` directly and calls the public functions.
  Patch `exchange_clock._now_ist` where needed to control the current time.

  1. `TestHolidayOverrideBlocksDefault`
     Cache: NON-MCX default (open 08:00–15:30, weekdays=[0,1,2,3,4]) + NON-MCX override
     (date=today, open_time=None, close_time=None). Time: 11:00 weekday.
     - `is_exchange_open("NSE")` → False
     - `get_today_gate_sessions("NON-MCX")` → []
     - `sessions_with_snapshot_time_now()` → no NON-MCX row

  2. `TestSpecialSessionCustomTimes`
     Cache: NON-MCX default + NON-MCX override (date=today, open_time=18:00, close_time=21:00).
     - At 19:00: `is_exchange_open("NSE")` → True (inside custom window)
     - At 14:00: `is_exchange_open("NSE")` → False (outside custom window; default suppressed)

  3. `TestWeekendDefaultRowBlocked`
     Cache: NON-MCX default (weekdays=[0,1,2,3,4]) only. Day: Saturday (weekday=5).
     - `is_exchange_open("NSE")` → False (no date override, weekday filter excludes Sat)

  4. `TestNoSnapshotOnHoliday`
     Cache: NON-MCX default (snapshot_time=15:45) + NON-MCX override (date=today, open_time=None).
     Time: 15:45.
     - `sessions_with_snapshot_time_now()` → empty (closed override suppresses snapshot)

  5. `TestMCXHolidayDoesNotAffectNSE`
     Cache: NON-MCX default (open 08:00–15:30) + MCX default (open 08:00–23:30) + MCX override
     (date=today, open_time=None). Time: 11:00 weekday.
     - `is_exchange_open("MCX")` → False
     - `is_exchange_open("NSE")` → True (NON-MCX default unaffected)

  6. `TestSpecialSessionSnapshotUsesCustomTime`
     Cache: NON-MCX default (snapshot_time=15:45) + NON-MCX override (date=today,
     open_time=18:00, close_time=21:00, snapshot_time=21:15). Time: 21:15.
     - `sessions_with_snapshot_time_now()` → returns override row (uses its own snapshot_time)

  7. `TestSpecialSessionNoSnapshotTimeSkipped`
     Cache: NON-MCX default + NON-MCX override (open_time=18:00, snapshot_time=None). Time: 15:45.
     - `sessions_with_snapshot_time_now()` → empty (no snapshot_time on override, not inherited)

  8. `TestSettlementCutoffUsesDefaultOpenTime`
     Cache: NON-MCX default (open_time=08:00). Called at 10:00.
     - `settlement_cutoff_for("NON-MCX")` → today 08:00 IST
     - Called at 07:00 → yesterday 08:00 IST

- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(exchange_clock): date-override-first; open_time=NULL means closed — remove is_open flag from session logic

## Done when

- `is_exchange_open("NSE")` returns False on a holiday override (open_time=NULL) during trading hours
- `is_exchange_open("MCX")` unaffected when only NON-MCX has a holiday override
- `sessions_with_snapshot_time_now()` empty for a gate with open_time=NULL override
- Weekend returns False with weekdays=[0,1,2,3,4] and no date override
- All pytest pass (broker ≥ 80%, api ≥ 45%)
