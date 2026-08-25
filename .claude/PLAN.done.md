# Plan: Fix daily_book prev_close — three-layer defect

## Context
`ltp == prev_close → day_change = 0` persists in both live and snapshot paths after both
markets close. Acceptable `ltp == prev_close` occurs ONLY after 08:00 IST on the next
market open day (new session started, no trades yet). Three bugs cause it.

**daily_book column semantics (key invariant):**
`daily_book.ltp` for date=Aug 24 = Aug 24 settlement = 98.00 (today's close).
`daily_book.previous_close` for date=Aug 24 = Aug 23 settlement = 97.50 (set by the
UPSERT rolling-shift at line 805 when NSE settlement fires and ltp changes 97.50→98.00).
The rolling-shift already stores the correct overnight baseline in `previous_close`. We just
weren't reading it.

---

**Bug 1 — live path cutoff (positions.py:878, holdings.py:376)**
`today_ist_cutoff = today_ist_8am if now_ist >= today_ist_8am else today_ist_midnight`
At 01:00 IST Aug 25: `today_ist_midnight = 00:00 IST Aug 25`. NSE EOD (15:35 IST Aug 24)
satisfies `captured_at < 00:00 IST Aug 25` → included → `prev_close = 98.00 = last_price → 0`.
Fix: `today_ist_midnight` → `today_ist_8am - timedelta(days=1)` in the else branch.

**Bug 2 — prev_ltp_map reads ltp instead of previous_close (daily_snapshot.py:1000–1008)**
`prev_ltp_map` query: `SELECT ... ltp FROM daily_book WHERE date < :today`.
At 00:15 IST Aug 25 (MCX settlement INSERT for date=Aug 25): this picks Aug 24 rows whose
`ltp = 98.00` (today's settlement). `previous_close = 98.00` stored in INSERT. `ltp = 98.00`.
Snapshot path: `previous_close = ltp → day_change = 0`.

Fix (no timestamp math needed — the rolling-shift already stores the right value):
- Before 08:00 IST: read `daily_book.previous_close` from `date < today` rows → 97.50 ✓
- At/after 08:00 IST: read `daily_book.ltp` from `date < today` rows → 98.00 ✓ (new session)

**Bug 3 — existing dirty data**
Today's rows (date=Aug 25) already have wrong `previous_close = ltp = 98.00`.
UPSERT rolling-shift cannot fix them (fires only when ltp changes, ltp is unchanged).
The correct values are already in yesterday's `daily_book.previous_close` (97.50) and `ltp` (98.00).
Need: one-time overnight fix (97.50) + 08:00 IST new-session fix (98.00).

---

## Task
Fix all three bugs across four files. No new data structures needed — use the values
already stored in `daily_book.previous_close` and `daily_book.ltp` from yesterday's rows.

## Agents
- backend: Implement all fixes.

  **Fix 1 — positions.py:878 and holdings.py:376 (one line each)**
  Change:
  ```python
  today_ist_cutoff = today_ist_8am if now_ist >= today_ist_8am else today_ist_midnight
  ```
  to:
  ```python
  today_ist_cutoff = today_ist_8am if now_ist >= today_ist_8am else today_ist_8am - timedelta(days=1)
  ```
  Update the comment block in both files:
  "Before 08:00 IST: cutoff = yesterday's 08:00 IST — excludes today's settlements so the
  query returns the prior-prior-session ltp (Aug 23 settlement), not today's (Aug 24 settlement)."

  **Fix 2 — daily_snapshot.py `prev_ltp_map` (lines 996–1009)**
  In `snapshot_daily_book`, add 08:00 IST check before the query (reuse `now_ist` from line 968):
  ```python
  _snap_midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
  _snap_8am = _snap_midnight + timedelta(hours=8)
  _before_session_open = now_ist < _snap_8am
  ```
  Then split the query into two cases:
  ```python
  if _before_session_open:
      # Overnight: read daily_book.previous_close from yesterday's rows.
      # The UPSERT rolling-shift already stored Aug 23 settlement (97.50) there
      # when NSE settlement fired and ltp shifted 97.50→98.00. Using it as
      # previous_close for today's INSERT gives the correct overnight baseline.
      _prev_sql = """
          SELECT DISTINCT ON (account, symbol, kind)
                 account, symbol, kind, previous_close AS ltp
          FROM daily_book
          WHERE date < :today
            AND previous_close IS NOT NULL AND previous_close > 0
            AND kind IN ('holdings', 'positions')
          ORDER BY account, symbol, kind, date DESC
      """
  else:
      # New session (>=08:00 IST): read daily_book.ltp from yesterday's rows.
      # ltp = prior-session settlement (Aug 24 settlement = 98.00) — correct
      # new-session baseline. ltp==prev_close at session open is valid.
      _prev_sql = """
          SELECT DISTINCT ON (account, symbol, kind)
                 account, symbol, kind, ltp
          FROM daily_book
          WHERE date < :today
            AND ltp IS NOT NULL AND ltp > 0
            AND kind IN ('holdings', 'positions')
          ORDER BY account, symbol, kind, date DESC
      """
  prev_ltp_map: dict[tuple[str, str, str], float] = {}
  try:
      async with async_session() as _sess:
          _prev_result = await _sess.execute(text(_prev_sql), {"today": target_date})
          prev_ltp_map = {
              (row.account, row.symbol, row.kind): float(row.ltp)
              for row in _prev_result
          }
  except Exception as _e:
      logger.warning("Snapshot: prev_ltp_map query failed (%s) — falling back to broker close_price", _e)
  ```
  Note: `timedelta` is already imported at the top of daily_snapshot.py.

  **Fix 3 — `fix_daily_book_prev_close` helper in daily_snapshot.py**
  Add after the `_upsert_rows` block, before the sparkline section:
  ```python
  async def fix_daily_book_prev_close(now_ist=None) -> int:
      """Repair daily_book.previous_close for today's rows.

      Overnight mode (now_ist < today's 08:00 IST):
        Reads yesterday's daily_book.previous_close (= prior-prior-session settlement,
        already correctly stored by the UPSERT rolling-shift mechanism).
        Updates only rows where previous_close ≈ ltp (wrong data).
        After fix: day_change = (today's settlement - prior-prior-session) × qty — shows
        yesterday's session performance during the closed-hours window.

      New-session mode (now_ist >= today's 08:00 IST, fires at 08:00 IST daily):
        Reads yesterday's daily_book.ltp (= prior-session settlement = yesterday's close).
        Updates today's rows unconditionally.
        After fix: previous_close = yesterday's settlement. ltp == prev_close is valid
        (no intraday movement yet). day_change = 0 correctly.
      """
      if now_ist is None:
          now_ist = timestamp_indian()
      midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
      today_8am = midnight + timedelta(hours=8)
      today = now_ist.date()

      if now_ist < today_8am:
          # Overnight: source = yesterday's previous_close; target = wrong rows only
          ref_col = "previous_close"
          ref_cond = "previous_close IS NOT NULL AND previous_close > 0"
          epsilon = 0.005   # only fix rows where previous_close ≈ ltp
          mode = "overnight"
      else:
          # New session: source = yesterday's ltp; target = all today rows
          ref_col = "ltp"
          ref_cond = "ltp IS NOT NULL AND ltp > 0"
          epsilon = 999999.0  # unconditional
          mode = "new-session"

      try:
          async with async_session() as session:
              result = await session.execute(text(f"""
                  WITH prev_ref AS (
                      SELECT DISTINCT ON (kind, account, symbol)
                             kind, account, symbol, {ref_col} AS ref_close
                      FROM daily_book
                      WHERE date < :today
                        AND {ref_cond}
                        AND kind IN ('holdings', 'positions')
                      ORDER BY kind, account, symbol, date DESC
                  )
                  UPDATE daily_book d
                  SET previous_close = r.ref_close
                  FROM prev_ref r
                  WHERE d.kind = r.kind
                    AND d.account = r.account
                    AND d.symbol = r.symbol
                    AND d.date = :today
                    AND d.ltp IS NOT NULL AND d.ltp > 0
                    AND ABS(COALESCE(d.previous_close, 0) - d.ltp) < :epsilon
              """), {"today": today, "epsilon": epsilon})
              await session.commit()
              updated = result.rowcount
          logger.info(
              "[PREV-CLOSE-FIX] mode=%s updated=%d rows (today=%s)",
              mode, updated, today,
          )
          return updated
      except Exception as e:
          logger.warning("[PREV-CLOSE-FIX] failed: %s", e)
          return 0
  ```
  Note: `timestamp_indian` and `timedelta` are already imported in this file.
  Note: using f-string is safe here — `ref_col` and `ref_cond` are hardcoded strings
  set within the function, never derived from external input.

  **Fix 4 — background.py `_task_daily_snapshot`**
  At the top of `_task_daily_snapshot`, add import alongside existing imports:
  ```python
  from backend.api.algo.daily_snapshot import snapshot_daily_book, fix_daily_book_prev_close
  ```
  (already imports `snapshot_daily_book` — add `fix_daily_book_prev_close` to same line)

  In the startup section (after the weekend/market-open checks, before `while True:`):
  ```python
  # One-time data repair: fix today's rows where previous_close = ltp (wrong).
  # Uses yesterday's daily_book.previous_close (correctly stored by rolling-shift)
  # so overnight display shows yesterday's session performance, not zero.
  try:
      await fix_daily_book_prev_close(_now_ist)
  except Exception as _e:
      logger.warning("Background: prev_close startup fix failed: %s", _e)
  ```

  Declare dedup var with the others (~line 1909):
  ```python
  _prev_close_fix_done: Optional[date] = None
  ```

  In the `while True:` loop, after the NSE settlement block, add:
  ```python
  # ---- 08:00 IST: transition previous_close to new-session baseline ----------
  # previous_close is set to yesterday's ltp (prior-session settlement).
  # ltp == prev_close at session open is valid — no intraday movement yet.
  if (now.time() >= dtime(8, 0) and now.time() < dtime(8, 30)
          and _prev_close_fix_done != today):
      logger.info("Background: 08:00 IST — daily prev_close new-session transition")
      try:
          await fix_daily_book_prev_close(now)
      except Exception as _e:
          logger.warning("Background: 08:00 IST prev_close fix failed: %s", _e)
      _prev_close_fix_done = today
  ```

  **Tests (mandatory — every changed file must have a test)**
  Write `backend/tests/test_daily_book_prev_close.py`:
  1. Cutoff formula (parametrized): time < 08:00 IST → `today_8am - 1day`; time >= 08:00 IST → `today_8am`.
  2. `fix_daily_book_prev_close` overnight mode: seed daily_book with
     date=yesterday rows (ltp=98, previous_close=97.5) and date=today rows
     (ltp=98, previous_close=98). Call with `now_ist` before 08:00.
     Assert today's rows get `previous_close = 97.5` (from yesterday's previous_close).
  3. `fix_daily_book_prev_close` new-session mode: same seed data, call with
     `now_ist` after 08:00. Assert today's rows get `previous_close = 98`
     (from yesterday's ltp). Today's rows had previous_close=97.5 from overnight fix;
     confirm they're overwritten unconditionally.
  4. `prev_ltp_map` overnight: mock daily_book with Aug 23 row (ltp=97.5, previous_close=97.0)
     and Aug 24 row (ltp=98, previous_close=97.5). Call `snapshot_daily_book` at 01:00 IST Aug 25.
     Assert prev_ltp_map returns 97.5 (Aug 24's previous_close), not 98 (Aug 24's ltp).
  5. `prev_ltp_map` new-session: same data, call at 09:00 IST.
     Assert prev_ltp_map returns 98 (Aug 24's ltp).

  For every file you change, you MUST write or update at least one test covering the
  changed behaviour. This is mandatory — not optional.

- frontend: skip
- broker: skip
- doc: skip
- backend-test: skip (backend agent covers tests)
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(daily_book): three-layer prev_close defect — cutoff, prev_ltp_map column, 08:00 session repair

## Done when
1. `positions.py:878` and `holdings.py:376` use `today_ist_8am - timedelta(days=1)` before 08:00 IST.
2. `prev_ltp_map` in `snapshot_daily_book` reads `daily_book.previous_close` before 08:00 IST
   and `daily_book.ltp` at/after 08:00 IST (both from `date < today` rows).
3. `fix_daily_book_prev_close()` in `daily_snapshot.py` exists with overnight + new-session modes.
4. `background.py` calls it on startup (one-time fix) and at 08:00 IST daily.
5. Pytest passes including new parametrized tests.
6. After MCX settlement (00:15 IST), today's daily_book rows have `previous_close = 97.5 ≠ ltp = 98`.
7. After 08:00 IST next trading day, same rows have `previous_close = 98 = ltp` → valid
   (new session, no trades yet). Day_change becomes non-zero once trading starts.
