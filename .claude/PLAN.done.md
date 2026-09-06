# Plan: fix MCX weekend snapshot_cutoff — Day P&L freezes wrong after settlement

## Context

**Why snapshots exist**: when market is closed, the positions route reads from `daily_book`
instead of calling the Kite API (session may have expired; `close_price` is stale until BHAV
arrives at ~08:00 next day). The snapshot stores the correct `ltp`, `day_pnl`, and
`previous_close` so the frozen state shown all weekend is accurate. `previous_close` is also
the baseline for the next session's Day P&L.

**Two MCX snapshots per Friday session**:
1. 23:30 — `trigger_close_snapshot("MCX")` → `date=Friday, ltp=23:30_last_tick`. This is
   necessary because the 15:30 NSE-close snapshot SKIPS MCX positions (`mid_session=True` at
   15:30 → MCX rows not written). Without it the 23:30–00:15 window would serve Thursday's data.
2. 00:15 — `trigger_settlement_capture("MCX")` → `date=Saturday, ltp=MCX_official_settlement`.
   The official settlement is a 30-min VWAP, can differ significantly from the 23:30 last tick.
   This is the authoritative Day P&L for the weekend.

**The bug**: `snapshot_cutoff = Saturday 00:00` in both positions.py and holdings.py. The 00:15
settlement has `captured_at = Saturday 00:15 > Saturday 00:00` → excluded from `latest_batch`.
Route falls back to the 23:30 close row (`ltp = last tick, not settlement`). Day P&L diverges.

Symptom: NavStrip shows 2.03L during Friday MCX session (live path, correct settlement
reference); on Saturday shows −13K (snapshot path, 23:30 last-tick reference). Gap ≈ 2.16L.

## Task

Extend Saturday and Sunday `snapshot_cutoff` by 2 hours (from midnight to 02:00 IST).
MCX settlement fires at 00:15; 02:00 safely includes it while still excluding any
hypothetical Saturday market-special-session (which would start at 09:00+ IST).

No change to `snapshot_daily_book`, `trigger_settlement_capture`, or `target_date`.

## Agents

- backend: Make the following two targeted changes:

  **File 1 — `backend/api/routes/positions.py` (lines ~231–236)**

  Change:
  ```python
  if _weekday == 5:   # Saturday
      _snapshot_cutoff = _today_ist_midnight
  elif _weekday == 6:  # Sunday
      _snapshot_cutoff = _today_ist_midnight - timedelta(days=1)
  ```
  To:
  ```python
  if _weekday == 5:   # Saturday: +2 h to capture MCX 00:15 settlement
      _snapshot_cutoff = _today_ist_midnight + timedelta(hours=2)
  elif _weekday == 6:  # Sunday: same boundary = Saturday 02:00 IST
      _snapshot_cutoff = _today_ist_midnight - timedelta(hours=22)
  ```
  Update the comment block (lines ~223–229) to reflect the new Saturday/Sunday cutoffs.

  **File 2 — `backend/api/routes/holdings.py` (lines ~108–113)**

  Change:
  ```python
  if _weekday == 5:   # Saturday
      snapshot_cutoff = _today_ist_midnight
  elif _weekday == 6:  # Sunday
      snapshot_cutoff = _today_ist_midnight - timedelta(days=1)  # Saturday 00:00
  ```
  To:
  ```python
  if _weekday == 5:   # Saturday: +2 h to capture MCX 00:15 settlement
      snapshot_cutoff = _today_ist_midnight + timedelta(hours=2)
  elif _weekday == 6:  # Sunday: Saturday 02:00 IST
      snapshot_cutoff = _today_ist_midnight - timedelta(hours=22)
  ```
  Update the adjacent comment.

  For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional.
  - `backend/api/` change → add/update a pytest test in `backend/tests/` covering the changed lines

  Add `backend/tests/test_mcx_settlement_snapshot_cutoff.py` with pure-formula tests (no DB needed):

  ```python
  """
  Regression guard: Saturday/Sunday snapshot_cutoff must reach Saturday 02:00 IST
  so the MCX 00:15 settlement row (captured_at=Saturday 00:15) is included in
  latest_batch, not excluded by the old Saturday 00:00 midnight cutoff.
  """
  from datetime import datetime, timedelta, timezone

  IST = timezone(timedelta(hours=5, minutes=30))

  def _compute_snapshot_cutoff(now_ist: datetime) -> datetime:
      """Mirror the cutoff logic from positions.py / holdings.py."""
      midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
      wd = now_ist.weekday()
      if wd == 5:    # Saturday
          return midnight + timedelta(hours=2)
      elif wd == 6:  # Sunday
          return midnight - timedelta(hours=22)
      else:
          return midnight + timedelta(days=1)

  # 2026-09-04 = Friday (weekday=4), 2026-09-05 = Saturday, 2026-09-06 = Sunday

  def test_saturday_cutoff_includes_mcx_settlement():
      sat_morning = datetime(2026, 9, 5, 9, 0, tzinfo=IST)
      cutoff = _compute_snapshot_cutoff(sat_morning)
      assert cutoff == datetime(2026, 9, 5, 2, 0, tzinfo=IST)
      mcx_settlement = datetime(2026, 9, 5, 0, 15, tzinfo=IST)
      assert mcx_settlement < cutoff, "MCX 00:15 settlement must be included in latest_batch"

  def test_saturday_old_cutoff_excluded_settlement():
      sat_morning = datetime(2026, 9, 5, 9, 0, tzinfo=IST)
      old_cutoff = sat_morning.replace(hour=0, minute=0, second=0, microsecond=0)
      mcx_settlement = datetime(2026, 9, 5, 0, 15, tzinfo=IST)
      assert mcx_settlement >= old_cutoff, "Regression: old midnight cutoff excluded settlement"

  def test_sunday_cutoff_includes_mcx_settlement():
      sun_morning = datetime(2026, 9, 6, 10, 0, tzinfo=IST)
      cutoff = _compute_snapshot_cutoff(sun_morning)
      expected = datetime(2026, 9, 5, 2, 0, tzinfo=IST)
      assert cutoff == expected
      mcx_settlement = datetime(2026, 9, 5, 0, 15, tzinfo=IST)
      assert mcx_settlement < cutoff

  def test_friday_cutoff_unchanged():
      fri = datetime(2026, 9, 4, 10, 0, tzinfo=IST)
      cutoff = _compute_snapshot_cutoff(fri)
      assert cutoff == datetime(2026, 9, 5, 0, 0, tzinfo=IST)  # tomorrow midnight

  def test_monday_cutoff_unchanged():
      mon = datetime(2026, 9, 7, 10, 0, tzinfo=IST)
      cutoff = _compute_snapshot_cutoff(mon)
      assert cutoff == datetime(2026, 9, 8, 0, 0, tzinfo=IST)

  def test_saturday_02h_cutoff_does_not_include_special_session():
      """Special market sessions start at 09:00+ IST — safely above 02:00 cutoff."""
      sat_special = datetime(2026, 9, 5, 9, 15, tzinfo=IST)
      sat_morning = datetime(2026, 9, 5, 9, 0, tzinfo=IST)
      cutoff = _compute_snapshot_cutoff(sat_morning)
      assert sat_special >= cutoff, "Saturday 09:15 special session excluded by 02:00 cutoff"
  ```

- frontend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(snapshot): extend Saturday/Sunday snapshot_cutoff to 02:00 IST — include MCX 00:15 settlement in latest_batch

## Done when
- Saturday `snapshot_cutoff = Saturday 02:00 IST` in both positions.py and holdings.py
- Sunday `snapshot_cutoff = Saturday 02:00 IST` (same boundary) in both files
- New pytest tests in `test_mcx_settlement_snapshot_cutoff.py` all pass
- On Saturday morning the positions route serves the MCX settlement row (00:15) instead of the 23:30 close row — Day P&L matches the value seen during the live session
