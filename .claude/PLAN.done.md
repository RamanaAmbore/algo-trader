# Plan: fix day P&L zero — holdings_policy + MCX overnight query cutoff

## Context

Two separate bugs both collapse day P&L to zero.

**Bug 1 — NSE holdings always zero (since commit 1a287553 removed backstop Fix 3)**

`holdings_policy` in `ltp_patch.py` has `if current > 0: return Decision()` — it never
patches `last_price` from KiteTicker when Kite REST returns a non-zero stale settlement.
`_override_stale_close_for_holdings` then computes `day_change_val = (stale_REST - daily_book.ltp) × qty`.
Since both the stale REST value and `daily_book.ltp` are yesterday's settlement, result is 0.
`positions_policy` uses an epsilon-aware comparison; `holdings_policy` must do the same.

**Bug 2 — Both positions AND holdings zero after MCX close (00:15–08:00 IST)**

`_override_stale_close_from_snapshot` (positions.py:873) and `_override_stale_close_for_holdings`
(holdings.py:374) both compute the daily_book query cutoff as:
```python
today_ist_cutoff = today_ist_midnight + timedelta(hours=8)  # 08:00 IST today
```

After MCX settles at 00:15 IST, `_snap_holding_eod_vals` writes a new `daily_book` entry with
`ltp = MCX settlement tonight`, `captured_at ≈ 00:15–00:30 IST`. This is BEFORE 08:00 IST, so the
`DISTINCT ON (account, symbol) … ORDER BY captured_at DESC` query picks it as the most-recent snapshot.

Consequence for **positions**: `snap_ltp = MCX_settlement_tonight`. Kite REST `close_price` =
`MCX_settlement_yesterday` (correct). `|snap_ltp − close_price| > 0.005` → close_price overwritten
with tonight's settlement. Now `last_price = close_price = MCX_settlement_tonight` → `day_change_val = 0`.

Consequence for **holdings**: `previous_close = MCX_settlement_tonight`. `last_price` from
Kite REST = same settlement. `day_change_val = (settlement − settlement) × qty = 0`.

**Fix for Bug 2**: when current IST hour < 8 (overnight window), use `16:00 IST yesterday` as cutoff
instead of `08:00 IST today`. MCX settlements always land between 23:30–00:30 IST (AFTER 16:00 IST
yesterday), so they are excluded. NSE settlements always land at ≈15:30 IST (BEFORE 16:00 IST), so
they are preserved. After 08:00 IST the regular cutoff applies and the MCX tonight snapshot is
correctly included as prev_close for the day's MCX session.

## Task

1. Fix `holdings_policy` in `ltp_patch.py` — prefer KiteTicker over stale REST (Bug 1).
2. Change daily_book query cutoff in `positions.py:_override_stale_close_from_snapshot` and
   `holdings.py:_override_stale_close_for_holdings` — use 16:00 IST yesterday during midnight-to-08:00
   IST window (Bug 2).
3. Add tests covering both fixes.

## Agents

- backend: skip
- frontend: skip
- broker: Make ALL of the following changes. No other changes.

  ---

  ### Fix 1 — `backend/api/helpers/ltp_patch.py` (lines 254–263)

  Replace `holdings_policy`:

  OLD:
  ```python
  def holdings_policy(current: float, tick_ltp: Optional[float]) -> Decision:
      if current > 0:
          return Decision()  # broker value is valid, leave it
      if tick_ltp is not None and tick_ltp > 0:
          return Decision(new_ltp=float(tick_ltp))
      return Decision(consider_cache=True)
  ```

  NEW (matches positions_policy epsilon pattern):
  ```python
  def holdings_policy(current: float, tick_ltp: Optional[float]) -> Decision:
      if tick_ltp is not None and tick_ltp > 0:
          if abs(tick_ltp - current) <= 0.005:
              return Decision()
          return Decision(new_ltp=float(tick_ltp))
      if current <= 0:
          return Decision(consider_cache=True)
      return Decision()
  ```

  ---

  ### Fix 2a — `backend/api/routes/positions.py` (around line 870–874)

  Replace the cutoff computation in `_override_stale_close_from_snapshot`:

  OLD:
  ```python
  today_ist_midnight = timestamp_indian().replace(
      hour=0, minute=0, second=0, microsecond=0,
  )
  today_ist_cutoff = today_ist_midnight + timedelta(hours=8)
  ```

  NEW:
  ```python
  now_ist = timestamp_indian()
  today_ist_midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
  # Invariant: prev_close is frozen until the next session opens at 08:00 IST.
  # Cutoff = the last 08:00 IST boundary that has passed.
  # Before 08:00 IST today: use yesterday's 08:00 IST → excludes tonight's MCX
  #   settlement snapshot (captured ≈ 00:15 IST today), which would otherwise make
  #   snap_ltp == last_price == MCX settlement → day_change_val = 0.
  # At/after 08:00 IST today: use today's 08:00 IST → new session started,
  #   tonight's MCX snapshot is now the correct prev_close for today's MCX session.
  today_ist_8am = today_ist_midnight + timedelta(hours=8)
  today_ist_cutoff = today_ist_8am if now_ist >= today_ist_8am else today_ist_8am - timedelta(days=1)
  ```

  Also replace the comment block just above (lines 858–867) explaining the cutoff rationale.

  ---

  ### Fix 2b — `backend/api/routes/holdings.py` (around lines 368–374)

  Replace the cutoff computation in `_override_stale_close_for_holdings`:

  OLD:
  ```python
  today_ist_midnight = timestamp_indian().replace(
      hour=0, minute=0, second=0, microsecond=0,
  )
  # 08:00 IST cutoff — same rationale as positions.py: MCX can land EOD
  # snapshots at 00:05 IST next calendar day; 08:00 IST is safely before
  # any mid-session startup snapshot.
  today_ist_cutoff = today_ist_midnight + timedelta(hours=8)
  ```

  NEW:
  ```python
  now_ist = timestamp_indian()
  today_ist_midnight = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
  # Invariant: prev_close is frozen until the next session opens at 08:00 IST.
  # Cutoff = the last 08:00 IST boundary that has passed.
  # Before 08:00 IST: use yesterday's 08:00 IST so tonight's MCX settlement
  #   snapshot (captured ≈ 00:15 IST today) is excluded. NSE closes at ≈15:30 IST
  #   (before any 08:00 IST boundary) and is always included correctly.
  today_ist_8am = today_ist_midnight + timedelta(hours=8)
  today_ist_cutoff = today_ist_8am if now_ist >= today_ist_8am else today_ist_8am - timedelta(days=1)
  ```

  Also update the existing comment block nearby to reflect the updated invariant.

  ---

  ### Tests

  Add or extend `backend/tests/broker/test_ltp_oscillation_fixes.py` (or create
  `backend/tests/broker/test_holdings_day_pnl.py`):

  **For Fix 1 (holdings_policy):**
  - `test_holdings_policy_prefers_ticker_over_stale_rest`: `holdings_policy(100.0, 102.5)` →
    `Decision(new_ltp=102.5)` (diff 2.5 > 0.005 → ticker wins)
  - `test_holdings_policy_noop_within_epsilon`: `holdings_policy(100.0, 100.002)` →
    `Decision()` (within epsilon → no-op)
  - `test_holdings_policy_cache_when_no_tick_zero`: `holdings_policy(0.0, None)` →
    `Decision(consider_cache=True)`
  - `test_holdings_policy_passthrough_when_no_tick_nonzero`: `holdings_policy(100.0, None)` →
    `Decision()`

  **For Fix 2 (prev_close only updates at 08:00 IST — last-boundary cutoff):**
  - `test_cutoff_before_8am_uses_yesterday_8am`: Mock `timestamp_indian()` to return 01:30 IST Aug 25.
    Verify cutoff = 08:00 IST Aug 24 (yesterday's 08:00 IST boundary). MCX tonight snapshot
    (captured 00:15 IST Aug 25 > 08:00 IST Aug 24) would be excluded; previous MCX settlement
    (00:15 IST Aug 24 < 08:00 IST Aug 24) included.
  - `test_cutoff_after_8am_uses_today_8am`: Mock `timestamp_indian()` to return 10:00 IST Aug 25.
    Verify cutoff = 08:00 IST Aug 25 (today's 08:00 IST boundary). Tonight's MCX snapshot
    (00:15 IST Aug 25 < 08:00 IST Aug 25) is included correctly as prev_close for today's MCX session.

  After edits run:
  ```bash
  cd /Users/ramanambore/projects/ramboq && venv/bin/pytest backend/tests/ -q --tb=short -k "ltp or holdings_day or mcx_overnight"
  ```

- doc: skip
- backend-test: skip
- playwright: no

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(positions,holdings): MCX overnight snapshot excluded from prev_close query; holdings_policy prefers ticker

## Done when
- `holdings_policy` returns `Decision(new_ltp=tick)` when tick differs from stale REST by > 0.005
- Between 00:15–07:59 IST: daily_book cutoff = 16:00 IST yesterday (MCX tonight snapshot excluded)
- Positions and holdings day P&L non-zero after MCX close (when price moved during session)
- pytest green

## Critical files
- `backend/api/helpers/ltp_patch.py` lines 254–263 (`holdings_policy`)
- `backend/api/routes/positions.py` lines 868–874 (`_override_stale_close_from_snapshot` cutoff)
- `backend/api/routes/holdings.py` lines 368–374 (`_override_stale_close_for_holdings` cutoff)
- `backend/tests/broker/test_ltp_oscillation_fixes.py` (or new test file)
