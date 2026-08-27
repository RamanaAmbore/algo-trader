# Plan: Fix 6d-audit findings — P1 pnl_per_share stale + P2 dedup/hoist/ceiling + P3 cleanup

## Task

Fix all findings from the 6d-audit in priority order:

**P1** — `holdings.py`: `_override_stale_ltp_from_ticker` updates `pnl` after LTP override but never
updates `pnl_per_share`. The derived column stays stale (shows old per-share figure) whenever
PriceBroker patches a zero-LTP Dhan/Groww row.

**P2a** — `_emit_conn_event` shim is copy-pasted verbatim into both `dhan.py` (lines 87–101) and
`broker_apis.py` (lines 19–33). Extract to `backend/brokers/conn_event_shim.py`; import from there.

**P2b** — `kite.py:place_gtt` loop (lines ~245–248): `from backend.shared.helpers.settings import get_int`
and `_mcx_gtt_ceiling = get_int(...)` are inside the `for _leg in orders:` loop — redundant per iteration.
Hoist both above the loop.

**P2c** — `groww.py`: `_GROWW_MARGINS_LOGGED: set[str] = set()` declared at line ~1574 (near EOF)
but referenced at line ~576. Move to top of file with other module-level state.

**P2d** — `dhan.py:place_gtt`: has no absurd-qty ceiling for NFO/BFO legs (Kite has a 50k-contract
ceiling). Add a parallel check before calling the Dhan API.

**P3a** — `positions_helpers.py`: `extract_snapshot_multiplier()` is deprecated (always returns 1,
no prod callers). Remove the function and flip the import test in `test_positions_imports.py`.

**P3b** — `base.py:validate_gtt_exchange`: empty method body silently no-ops if an adapter forgets
to override. Add `pass` + a comment explaining the "all exchanges allowed" default intent.

**P3c** — `groww.py`: NCO exchange missing from `_EXCHANGE_TO_GROWW` and `_SEGMENT_TO_GROWW`.
Add it (NCO = National Commodity Options, same tier as MCX).

## Agents

- backend: skip
- frontend: skip
- broker: Fix all eight findings:
  1. `backend/api/routes/holdings.py` (~line 312–328): after `raw.loc[_sel, 'pnl'] = _pnl_p`,
     add `raw.loc[_sel, 'pnl_per_share'] = (_pnl_p / _qty_p.replace(0, float('nan'))).fillna(0)`
     inside the same `if _ltp_p > 0:` block.
  2. Create `backend/brokers/conn_event_shim.py` with the single shared `_emit_conn_event` shim;
     replace both copies in `dhan.py` and `broker_apis.py` with `from backend.brokers.conn_event_shim import _emit_conn_event`.
  3. `backend/brokers/adapters/kite.py`: hoist `get_int` import and `_mcx_gtt_ceiling` above the
     `for _leg in orders:` loop in `place_gtt`.
  4. `backend/brokers/adapters/groww.py`: move `_GROWW_MARGINS_LOGGED: set[str] = set()` from
     ~line 1574 to the top of the file, grouped with other module-level set/dict state.
  5. `backend/brokers/adapters/dhan.py`: in `place_gtt`, add a ceiling check for NFO/BFO legs
     (≤ 50,000 contracts) mirroring `_check_kite_gtt_qty_ceiling` pattern; raise `ValueError` on breach.
  6. `backend/api/routes/positions_helpers.py`: delete `extract_snapshot_multiplier()` function.
  7. `backend/brokers/base.py`: add explicit `pass` to `validate_gtt_exchange` body and a one-line
     comment: `# Subclasses raise ValueError for unsupported exchanges; default = all allowed.`
  8. `backend/brokers/adapters/groww.py`: add `"NCO"` to `_EXCHANGE_TO_GROWW` and `_SEGMENT_TO_GROWW`.
  For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional.
  - `backend/brokers/` change → add/update a pytest test in `backend/tests/broker/` covering the changed lines
  - `backend/api/` change → add/update a pytest test in `backend/tests/` covering the changed lines
  No change ships without a corresponding test update.
- doc: skip
- backend-test: skip
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(audit): P1 pnl_per_share stale after LTP override; P2 dedup _emit_conn_event + hoist get_int + Dhan GTT ceiling + Groww margins set; P3 drop extract_snapshot_multiplier + base.py comment + NCO exchange

## Done when

- `holdings.py`: `pnl_per_share` is updated whenever `pnl` is rewritten by `_override_stale_ltp_from_ticker`
- `conn_event_shim.py` exists; neither `dhan.py` nor `broker_apis.py` contains a local copy of the shim
- `kite.py place_gtt`: `get_int` import and `_mcx_gtt_ceiling` assignment are above the `for _leg` loop
- `groww.py`: `_GROWW_MARGINS_LOGGED` is declared at the top of the file
- `dhan.py place_gtt`: NFO/BFO qty ceiling check exists and raises on breach
- `positions_helpers.py`: `extract_snapshot_multiplier` is gone
- `base.py validate_gtt_exchange`: has explicit `pass` + comment
- `groww.py`: `NCO` is in both mapping dicts
- All pytest tests pass (broker ≥ 80%, API ≥ 45%)
