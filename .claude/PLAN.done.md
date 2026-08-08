# Plan: firm NAV snapshot overlay — sync with holdings grid cur_val

## Context

`compute_firm_nav()` calls raw `fetch_holdings()` which uses broker REST `cur_val`
(pre-session/stale prices). The `/api/holdings` route applies
`_overlay_snapshot_for_closed_exchanges()` which replaces `cur_val` with
`DB snapshot LTP × qty` for currently-closed exchanges. Result: firm NAV shows
~6L lower than the holdings grid during pre-open / post-close windows — a visible
SSOT break on the performance page.

Root cause confirmed by audit: `latest_snapshot_ltp_map()` and
`is_exchange_closed_now()` exist in `backend/api/helpers/snapshot_gate.py` but
are NOT used by `nav.py`. The route layer uses them; the algo layer doesn't.

---

## Task

Apply the same closed-exchange LTP overlay to `compute_firm_nav()` so firm NAV
and the holdings grid read the same `cur_val` for each holding when an exchange
is closed.

Also fix two secondary issues found by the audit:
- `_funds_from_df` dead primary column paths (stale pre-rename names never hit)
- `_positions_from_df` structural inconsistency vs frontend (qty != 0 gate)

---

## Agents

- backend: In `backend/api/algo/nav.py`:

  **Primary fix — `_fetch_holdings_phase`** (line ~307):
  1. Add import at top of `_fetch_holdings_phase`: `from backend.api.helpers.snapshot_gate import is_exchange_closed_now, latest_snapshot_ltp_map`
  2. Before iterating dfs, call `snap_map = await latest_snapshot_ltp_map("holdings")`
  3. For each df, before calling `_holdings_from_df(df, ticker)`, call a new private helper
     `_overlay_closed_exchange_ltp(df, snap_map)` that:
     - Iterates rows of the df (pandas)
     - For each row: if `is_exchange_closed_now(row["exchange"])` is True, look up
       `snap_map.get((str(row["account"]), str(row["tradingsymbol"])))` → if found and > 0,
       set `df.at[idx, "cur_val"] = snap_ltp * float(row["quantity"] or 0)`
     - Returns the modified df (copy first to avoid mutating the original)
     - Guard: return df unchanged if `df.empty` or `not snap_map`

  **Secondary fix — `_funds_from_df`** (line ~72):
  Read the function. The primary column names (`avail opening_balance`,
  `util option_premium`) are dead code since `_COL_MAP` in funds.py renamed them.
  Only the fallback branch (`cash`, `option_premium`) executes. Remove the stale
  primary names; keep only the fallback branch, simplifying the function.

  Write/update tests in `backend/tests/test_firm_nav_overlay.py` (new file):
  - Test 1: `_overlay_closed_exchange_ltp` replaces `cur_val` with `snap_ltp × qty`
    when exchange is closed (mock `is_exchange_closed_now` → True)
  - Test 2: `_overlay_closed_exchange_ltp` does NOT replace `cur_val` when exchange
    is open (mock `is_exchange_closed_now` → False)
  - Test 3: `_overlay_closed_exchange_ltp` is a no-op when `snap_map` is empty
  - Test 4: `_overlay_closed_exchange_ltp` is a no-op when df is empty

- broker: skip
- frontend: skip
- doc: skip
- backend-test: skip
- playwright: skip

---

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

---

## Commit message
fix(nav): apply closed-exchange snapshot overlay in compute_firm_nav to match holdings grid cur_val

## Done when
- `_fetch_holdings_phase` calls `latest_snapshot_ltp_map("holdings")` and applies
  `_overlay_closed_exchange_ltp` before summing `cur_val`
- `_overlay_closed_exchange_ltp` exists as a private function in nav.py
- `_funds_from_df` dead primary column names removed
- 4 new pytest tests pass for overlay behavior
- pytest green overall, broker cov ≥ 80%
