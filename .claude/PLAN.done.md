# Plan: Day P&L / Exp P&L Redesign (baseline-diff formula, account/symbol rollups)

## Context

An audit of Day P&L, cash, and Exp P&L calculations across Kite/Dhan/Groww surfaced five confirmed bugs in the current per-position formula machinery (stale cross-day P&L bleed into new positions, realized P&L missing from NAV on same-day exits, closed-market frontend recompute, a fragile blended-average fallback formula, MCX lot/contract mismatch in the snapshot reader) plus three confirmed bugs in the separate Exp P&L (expiry-projected P&L) calculation (weekly-symbol strike misparse, futures valued at own LTP instead of spot, two disagreeing partial-close implementations depending on an unconfirmed Kite field behavior).

Working through the correct design with the operator established: Day P&L for any position, regardless of state (new entry, full exit, partial exit, re-entry, flip, or any combination), reduces to one formula — `current_total_profit − base_pnl`, where `base_pnl` is that position's total profit frozen at the most recent trading day's close-reset snapshot (0 if none exists). This is proven algebraically correct and needs no branching by position-state. The operator also decided: per-position Day P&L is no longer displayed anywhere in the UI — only account-level and symbol-level (across accounts) rollups, plus grand totals. Holdings keeps its existing (already mostly-correct) calculation path unchanged, just fixed where broken — it is not unified into the new positions mechanism.

## Design summary

- **Atomic formula (positions)**: `current_total_profit = realised + unrealised` (never a broker's combined `pnl` field, to avoid double-counting — except **Kite**, where the combined `pnl` field is now confirmed via Zerodha's own forum statement to equal `realised + unrealised` exactly and is the officially recommended field going forward; use Kite's native `pnl` directly). `Day P&L = current_total_profit − base_pnl`.
- **Per-broker sourcing**:
  - Kite: native `pnl` directly (confirmed = realised+unrealised, Zerodha-recommended; the split `realised`/`unrealised` fields are flagged by Zerodha as possibly unreliable/deprecated).
  - Groww: native `pnl` when present (mirrors Kite); fallback to `realised_pnl + unrealised_pnl` when `pnl` absent.
  - Dhan: no trustworthy native combined field — `realised_pnl` (native, trustworthy) + `(ltp − average_price) × qty` (locally derived; both inputs confirmed broker-authoritative).
- **Baseline**: `daily_book.total_pnl`, already stored per `(date, account, kind, symbol)`. Fix the lookup (`_fetch_snapshot_close_map`, `backend/api/routes/positions.py:922-970`) to bound to the exact most-recent trading day per account (reuse the existing `latest_batch`-per-account CTE pattern already used in `_positions_snapshot`, `positions.py:247-270`) instead of today's unbounded `captured_at < today_08 ORDER BY DESC` — a symbol not present in that batch correctly defaults to `base_pnl = 0` rather than reaching back to an arbitrarily old stale row.
- **Aggregation only — no per-position display**: sum the atomic value per account (extend `_build_polars_summary`, `positions.py:575-606`, already produces account+TOTAL rows) and per symbol across accounts (new parallel rollup, same groupby pattern). Nothing renders a per-row Day P&L anywhere.
- **Holdings**: unchanged mechanism — `(ltp − prev_close) × qty` using `daily_book.ltp` as `prev_close` (already correct, COALESCE bug already fixed), with broker-native day-change preferred first where reliable (Kite `day_change`, Groww `day_change`; Dhan has no native absolute-₹ field, always computed). The snapshot/closed-hours path (`_compute_holding_day_change`, `holdings.py:130-153`) already implements this native-first-then-fallback priority correctly — the live (non-snapshot) path needs to be brought in line with the same priority (verify during implementation).
- **NAV**: `compute_firm_nav`'s `_positions_from_df` (`nav.py:121-165`) currently sums only `unrealised`, gated to `quantity != 0` — so a same-day full exit's realized profit never reaches NAV. Fix: sum `current_total_profit` (unrealised, still qty-gated fine since it's legitimately 0 on flat rows, **+ realised, ungated**) per account. Uses `current_total_profit` only, never `baseline_diff_day_pnl` — NAV wants lifetime total profit, not a day-over-day delta.
- **Exp P&L**: separate metric (projected P&L if held to expiry), audited and confirmed broken independent of the Day P&L work — fix in the same pass since both touch overlapping position-state/broker-field logic.
- **New UI field**: `overnight_quantity` is backend-available (confirmed populated for all three brokers) but shown nowhere. Add a short-labeled column ("O/N Qty") to every position grid.

## Backend changes

**`backend/api/algo/pnl_math.py`** — add `current_total_profit(realised, unrealised)` and `baseline_diff_day_pnl(realised, unrealised, base_pnl)` as the new SSOT, plus vectorised pandas/polars wrappers so both the route path and `broker_apis.py`'s polars enrichment call the same logic. Existing `decomposed_intraday_pnl`/backstop machinery may stay for diagnostic purposes but must no longer feed rollups or displays.

**Four places currently compute total profit from the ambiguous/wrong field — converge all onto the new helper**:
1. `backend/brokers/broker_apis.py:2160-2176` (`_pnl_expr = broker_pnl + broker_realised`) — double-counts for Kite since `pnl` already includes `realised` (confirmed via Zerodha forum). Fix per the per-broker sourcing above.
2. `backend/api/algo/daily_snapshot.py:551-554` (`_snap_position_eod_vals`, writes `daily_book.total_pnl` — this **is** tomorrow's `base_pnl`) — same fix, reads raw broker payload independently of #1.
3. `daily_snapshot.py` holdings-analog EOD writer (~line 692) — leave holdings' own mechanism as-is per operator scope decision; only touch if the audit finds it broken.
4. `backend/api/background.py` (`_fetch_positions_direct`, ~267-330) — a third independent rollup feeding the SSE/polling path; must converge on the same helper or the app will show disagreeing numbers depending on which path served last.

**Baseline query fix** (`_fetch_snapshot_close_map`, `positions.py:922-970`): apply the batch-anchored bound (see Design summary) to the `total_pnl`/`prev_pnl_map` half only. Leave `prev_close`/`snapshot_map` sourcing untouched (governed by the separate documented "close_price / ltp invariant — DO NOT CHANGE" rule) — flag the same latent staleness risk there as a follow-on, not in this change. Apply the equivalent bound to `_positions_snapshot`'s `prev_batch` CTE (currently a looser 7-day window with the same per-symbol drift risk) — factor both into one shared helper so the live and closed-hours paths can't disagree.

**Holdings-sold-into-positions correctness requirement**: per the existing "Holdings sold → P&L splits" rule, when a holding is sold, realized P&L for that qty lives on the resulting CNC positions row. If the new positions baseline join is scoped to `kind='positions'` only, that row gets `base_pnl=0` (wrong — it was a holding yesterday) and shows its full lifetime gain as "today's" P&L. Fix: the baseline join for this case must check `kind IN ('positions','holdings')` for that `(account,symbol)`, sourcing `base_pnl` from whichever kind held the position at the prior close-reset. Add a dedicated test.

**Rollup response shape**: extend `_build_polars_summary` and `PositionsResponse.summary` (`backend/api/schemas.py:221-236`) to compute the account-level total via `baseline_diff_day_pnl`. Add a new parallel symbol-level rollup (`_build_polars_symbol_summary`, grouped by `tradingsymbol` across accounts) and a `PositionsResponse.symbol_summary` field, wired into all response-building call sites in `positions.py`.

**NAV** (`backend/api/algo/nav.py:121-165`): per Design summary. Add a `TestNavMultiAccount`-style case (`backend/tests/test_nav_formula.py:482`) for "closed-today position with nonzero realised, qty=0". Flag for operator: whether this double-counts against `cash_total`'s `option_premium` term depends on unconfirmed Kite behavior — needs one empirical live-account check before treating this as fully verified.

**Exp P&L fixes** (core: `frontend/src/lib/data/expiryPnl.js`, consumed via two diverging paths — `frontend/src/lib/data/portfolioStore.svelte.js:118-135` for Pulse/NavStrip/Snapshot, and `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` + `lib/derivatives/pageLoad.js` for the Legs grid):
- Fix the weekly-symbol strike-parsing regex (`expiryPnl.js:46`) — reuse `decomposeSymbol.js`'s already-correct weekly-symbol parser instead of the broken inline regex.
- Fix futures valuation to use spot, not the future's own LTP (`portfolioStore.svelte.js:128-130`), matching the documented spec and the column tooltip.
- Unify the two diverging partial-close implementations (Pulse's `ev + realised` vs. Legs' `splitClosedReopened`-based split) onto one shared function — resolve first via one live-account check whether Kite's `average_price` after a partial close is cost-basis or breakeven-folded (this determines which of the two current implementations, if either, is currently correct).
- Add the missing split for `overnight_quantity == 0` (intraday partial closes, and all Groww rows — Groww hardcodes `overnight_quantity=0`), which currently silently drops the realized portion.
- Fix provisional MCX fill quantity (lots→contracts) before display (`backend/api/routes/orders.py:456-470`).
- Delete dead code `rawPosExpPnl` (`derivativesMath.js:532-552`) and its tests, which test a path nothing calls.
- Fix stale tooltips (`pulseColumns.js:746,773`, `PositionStrip.svelte:712`) to match actual behavior.

## Frontend changes

**Formula consolidation** — `frontend/src/lib/data/nav.js`: collapse `baseDayPnlForPosition`/`livePositionDayPnl`'s branchy Case 1-4 logic into `(realised + unrealised) − prev_settlement_pnl`, plus a live-tick delta applied to `unrealised` directly. This feeds the existing `portfolioStore.svelte.js` aggregation structures (`byKey`, `byAccount`, `total`) unchanged in shape — only the per-row formula changes.

**Remove per-position Day P&L display, add symbol/account rollups**, consumer by consumer:
- `frontend/src/lib/data/pulseColumns.js:601-604` — delete the per-row `day_pnl` colDef. Keep/extend `mkPosSummaryCols`/`mkHoldSummaryCols` (account-level, already exist); add a new `mkSymbolSummaryCols` for the new symbol-level rollup.
- `frontend/src/lib/MarketPulse.svelte` — remove the per-row `day_pnl` valueGetter patch, flash-update wiring, and CSV export columns; re-source TOTAL-row and pinned-summary-row values from `portfolioStore.positions.byAccount`/`.total` directly (not from per-row data, which is going away) — must land in the same commit as the colDef removal.
- `frontend/src/lib/PositionStrip.svelte`, `NavCard.svelte`, `NavBreakdown.svelte` — already read aggregates; verify (residual check during implementation) no per-position derivation is inlined anywhere in these files.
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` + `CandidateLegRow.svelte` (heaviest change — the Legs and Expiry tabs share one CSS-subgrid component, `.cand-grid`): remove per-leg Day P&L cell, header span, and TOTAL-row cell together with the CSS grid-template-columns track in one commit (subgrid breaks if header/row/TOTAL/CSS column counts disagree). Reuse the existing `.byund-grid` (by-underlying, root-level) summary table as the symbol-level rollup surface rather than building a new one, unless per-exact-contract granularity is explicitly wanted. Redefine per-row "Chg %" as a pure price-% (`(ltp−prevClose)/prevClose×100`) rather than removing it, since it's a distinct, still-meaningful metric once decoupled from the P&L baseline.
- `frontend/src/routes/(algo)/dashboard/+page.svelte` — holdings cards are already symbol-grouped aggregates; verify they read from the corrected formula, no structural rewrite expected.
- Apply the same Exp P&L fixes (weekly-symbol parsing, futures-at-spot, unified partial-close logic) across all consuming surfaces (Pulse grid, NavStrip P3, NavBreakdown, derivatives Snapshot/Legs).

**New `overnight_quantity` ("O/N Qty") column**:
- `frontend/src/lib/data/pulseColumns.js` — add adjacent to `qty_net` (line 583-587); confirm whether pulse's unified/cross-account row shape needs a summing valueGetter (mirroring `_qtyNetValueGetter`) rather than a flat field read — verify during implementation.
- `admin/derivatives/+page.svelte` + `CandidateLegRow.svelte` — add header span, row span, CSS grid track together (same subgrid-alignment constraint as above). Confirm `expiryCloseAnalysis` (the Expiry tab's client-side row data) carries `overnight_quantity` through — verify during implementation, not confirmed in research.

## Tests

**Backend (pytest)**: extend `test_pnl_math_ssot.py` / `broker/test_pnl_math.py` with unit tests for `current_total_profit`/`baseline_diff_day_pnl` covering new/full-exit/partial-exit/re-entry/flip/holdings-to-CNC-split. Update `test_day_pnl_contracts.py`, `test_universal_day_pnl.py`, `test_positions_pnl_split.py`, `test_market_window_pnl_edge_cases.py` for the new formula; add a case proving the bounded baseline query returns 0 for a symbol absent from the most recent per-account batch. Extend `test_nav_formula.py::TestNavMultiAccount` for the closed-today-realized case. Add a regression test asserting `_snap_position_eod_vals` and `_enrich_positions` produce identical `total_profit` for the same synthetic row (guards against the two implementations drifting again).

**Frontend (Playwright + Vitest)**: rewrite the ~10 specs currently asserting per-position Day P&L SSOT (`day_pnl_ssot.spec.js`, `navbreakdown_daypnl_ssot.spec.js`, `pnl_positions_closed_hours_ssot.spec.js`, `derivatives_day_pnl_health.spec.js`, `derivatives_pulse_day_pnl_ssot.spec.js`, `pulse_pinned_and_navstrip_day_pnl.spec.js`, etc.) to assert account/symbol rollup totals AND that no per-row Day P&L cell exists. Redesign or remove `day_pnl_breakup.spec.js`'s modal coverage per its UI's disposition (decide during implementation). Add weekly-symbol, partial-close, and MCX-unit cases to `expiryPnl.test.js`/`derivativesMath.test.js` (currently only toy-symbol coverage); delete dead-code tests for `rawPosExpPnl`. Update `portfolioStore.test.js`/`positionsDerivedStore.test.js`, which currently re-implement logic inline instead of testing the real code path (`positionsDerivedStore.test.js:288` asserts the wrong futures-at-LTP behavior — must flip to spot-based).

## Docs

Update `CLAUDE.md`'s "Day P&L reference price by row type" / "Day P&L formulas by position type" / "Frontend Day P&L SSOT" sections to describe the new formula and the account/symbol-only display contract — mark as superseding, not silently deleting, since they document real past incidents. Explicitly confirm the "close_price / ltp invariant" and "Holdings sold → P&L splits" rules are unchanged in contract (only their implementation detail changes per the kind-join fix). Update `docs/specs/PULSE_SPEC.md` (currently only documents the Legs Exp P&L path, not Pulse/NavStrip/Snapshot).

## Flagged decisions (apply stated defaults; operator may override at any point)

1. `daily_book.total_pnl` writer-fix cutover: ship the writer fix and let one EOD close-reset cycle run before flipping the reader formula live (default), vs. accept a one-day baseline glitch.
2. NAV `option_premium` vs. same-day-realized double-count — needs one empirical live-account check (buy+sell same symbol same session, compare NAV before/after) before sign-off.
3. Derivatives symbol-rollup granularity: default to root-level (reuse existing `.byund-grid`), not per-exact-contract.
4. Exp P&L partial-close unification: needs one empirical live-account check (Kite `average_price` cost-basis vs. breakeven-folded after a partial close) before the fix can be finalized with certainty.

## Verification

- `venv/bin/pytest backend/tests/ -q --tb=line` — full backend suite green, including new/updated P&L, NAV, and holdings tests.
- `cd frontend && npx svelte-check --output machine` — zero errors across all edited `.svelte`/`.js` files.
- `cd frontend && npx vitest run` — updated store/formula unit tests green.
- `cd frontend && npx playwright test` — rewritten SSOT specs green, explicitly asserting absence of per-row Day P&L cells on Pulse/derivatives grids.
- Manual: one live-account empirical check each for (a) Kite `average_price` behavior after a partial close, (b) NAV `option_premium` vs. realized-P&L overlap, before final sign-off on the Exp P&L and NAV pieces respectively.
