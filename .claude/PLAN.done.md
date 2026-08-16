# Plan: Fix Day P&L race + St column heading + orphan value

## Context

Three persistent defects after multiple deploy cycles:

1. **Day P&L race condition** — positions Day P&L shows 0 on first page load, correct after refresh. Root cause: the LTP gate in `broker_apis.py:1910` zeroes `day_change_val` when `last_price=0` (mmap not warmed yet). The backend backstop in `pnl_math.py` rescues Case 1 (new position) and Case 3 (flat intraday) but is missing **Case 2**: overnight position (`oq>0`) where the gate zeroed dcv but broker pnl is non-zero. This is the dominant race for Dhan and Groww overnight positions.

2. **Holdings Day P&L (Dhan/Groww)** — Holdings adapters produce a scalar `day_change = ltp − close` in the row dict. `_enrich_holdings()` correctly recomputes `dcv = pnl − (close−avg)×qty` but when backfill fails to resolve a symbol, dcv stays at 0. Adding a post-enrichment fallback using the adapter's scalar when `close_price > 0` plugs this gap.

3. **St column heading missing in derivatives** — `+page.svelte:4469` has `<span title="..."></span>` with no visible text. Confirmed empty span.

4. **St value not showing as orphan** — User requires `○` for all unmatched positions in pulse positions, legs, and exp-close (no pair groups currently active). Current conditional `d.quantity !== undefined` is fragile; simplify to `!d.has_gtt && !d.pair_group_key → '○'` unconditionally.

## Task

Four targeted fixes:
1. Add Case 2 to `apply_day_change_backstop()` in `pnl_math.py`
2. Add `day_change × opening_quantity` fallback in `_enrich_holdings()` for Dhan/Groww
3. Insert "St" text in the derivatives header span at `+page.svelte:4469`
4. Simplify St cell renderer to always show `○` when not GTT and no pair group

## Agents

- backend: Add Case 2 to `apply_day_change_backstop()` in `backend/api/algo/pnl_math.py`:
  Read `close_price` and `average_price` from raw (lines 176-187 pattern). Add `_cls` and `_avg` series with fillna(0). Add `_case2 = (_oq > 0) & (_dcv == 0) & (_pnl != 0) & (_cls > 0) & (_avg > 0)`. Compute `_case2_val = _pnl - (_cls - _avg) * _oq`. Extend mask to `_case1 | _case2 | _case3`. For the write-back, set Case 1+3 to `_pnl`, and Case 2 to `_case2_val` (not pnl — different recovery formula). Also add `day_change × opening_quantity` fallback in `broker_apis.py:_enrich_holdings()` (around line 1641, after the polars write-back loop): when `day_change_val == 0` AND `day_change` column present AND `close_price > 0` AND `opening_quantity != 0`, set `day_change_val = day_change × opening_quantity` using pandas. This handles Dhan holdings where the backfill path failed to resolve the symbol.

- frontend: Two fixes:
  1. `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` line 4469 — change `<span title="Position state: GTT (green) / Paired (cyan) / Orphan (amber)"></span>` to `<span title="Position state: GTT (green) / Paired (cyan) / Orphan (amber)">St</span>`
  2. `frontend/src/lib/data/pulseColumns.js` — in the `pos_state` column `cellRenderer` (around line 485-493), replace the `if (d.is_orphan) return '○'; if (d.quantity !== undefined) return '○';` lines with a single unconditional `return '○';` after the GTT and pair_group_key guards. Final logic: `if (!d || d._isTotal) return ''; if (d.has_gtt) return 'GTT'; if (d.pair_group_key) return d.pair_group_key; return '○';`
  3. `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte` — at line 204 (St cell), simplify to: `{c.has_gtt ? 'GTT' : c.pair_group_key ?? '○'}` — always show orphan when no pair group and no GTT.

- backend-test: Add tests for the two backend changes:
  1. In `backend/tests/test_positions_route.py` or a new `backend/tests/broker/test_pnl_math.py`: test Case 2 in `apply_day_change_backstop` — overnight position with `oq=10, dcv=0, pnl=500, close=100, avg=95` → `dcv = 500 - (100-95)*10 = 450`.
  2. In `backend/tests/broker/test_holdings_dcv.py` (new): test Dhan holdings fallback — row with `day_change=5.0, opening_quantity=100, close_price=950, day_change_val=0` → after `_enrich_holdings`, `day_change_val = 500`.

- doc: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(pnl): Case 2 backstop for overnight positions + Dhan holdings dcv fallback + St header + orphan value

## Done when
- Overnight positions (Dhan/Groww) show non-zero Day P&L on first page load
- Holdings Dhan Day P&L shows correctly when backfill symbol resolution fails
- Derivatives legs and exp-close show "St" column heading
- All positions in pulse, legs, and exp-close show "○" in St column when no pair group is active
- pytest passes with new Case 2 test and holdings dcv fallback test
- svelte-check 0 errors
