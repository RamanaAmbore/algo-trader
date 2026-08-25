# Plan: Reduce CC of three D-grade hotspots to unblock prod merge

## Context

dprod CC gate blocked prod merge because three functions remain D-grade (CC ≥ 16).
The previous commit (b33d056b) is on dev and ready to merge; only the CC block prevents it.
Goal: extract helpers to bring each below D (CC ≤ 15) — no behaviour change, tests unchanged.

Pre-existing hotspots (not introduced by b33d056b):
- `backend/api/background.py:1817  _task_daily_snapshot`  D (30) — improved from E(33) last session
- `backend/api/algo/daily_snapshot.py:600  _positions_rows`  D (22)
- `backend/api/routes/orders.py:1728  OrdersController.order_postback_groww`  D (24)

Pattern: same as commit 28156f2a ("refactor(cc): extract helpers from _enrich_holdings + chain_quotes").

## Task

Extract private helper functions from each of the three D-grade functions to lower CC below 16.
No logic changes. Keep all existing tests green.

## Agents

- backend: Refactor three functions to lower CC. For each, extract cohesive sub-blocks into
  private helpers until `radon cc -s -n D` no longer lists the parent function.

  **`backend/api/background.py` — `_task_daily_snapshot` (CC=30)**
  Read the function. Extract logical sub-phases (e.g., pre-market checks, snapshot trigger
  branches, each broker-type path) into private `_snapshot_*` helpers in the same file.
  Target: parent CC ≤ 15.

  **`backend/api/algo/daily_snapshot.py` — `_positions_rows` (CC=22)**
  Read the function. Extract filtering/transformation branches into private helpers.
  Target: parent CC ≤ 15.

  **`backend/api/routes/orders.py` — `OrdersController.order_postback_groww` (CC=24)**
  Read the function. Extract fill-detection logic and per-state processing into private
  `_groww_*` helpers or a module-level helper function.
  Target: parent CC ≤ 15.

  After each extraction, verify with:
  `venv/bin/python -m radon cc backend/ -s -n D 2>/dev/null`
  Must produce no output (zero D/E/F functions).

  For every file you change, confirm existing tests still cover the extracted logic
  (no new tests needed if helpers are pure extractions — but if the extaction moves a
  branch that was previously tested indirectly, add a targeted test).

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
refactor(cc): extract helpers from _task_daily_snapshot, _positions_rows, order_postback_groww to restore grade C

## Done when
- `venv/bin/python -m radon cc backend/ -s -n D 2>/dev/null` produces no output
- All pytest tests still green
- dprod can proceed: CC gate unblocked → merge dev→main
