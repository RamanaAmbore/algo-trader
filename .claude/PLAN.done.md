# Plan: Round 14 — three frontend bug fixes (snapshot dropdown, exp-close row tint, NavStrip P slot)

## Task

Fix three distinct bugs found in the derivatives page and NavStrip:

1. **Snapshot card account dropdown shows no options** — `accountChoices` in
   `derivatives/+page.svelte` is derived from both `positions` and `realAccounts`, but the
   dropdown appears empty. Investigate whether `{#key accountChoices.length}` remount is
   breaking the bind or whether `realAccounts`/`positions` timing is the root cause, and fix.

2. **Exp-close grid missing alternating row tint** — `.cand-row.expiry-band-netted` and
   `.cand-row.expiry-band-otm` in `CandidateLegRow.svelte` declare
   `background-color: transparent` which has higher CSS specificity than the global
   `.row-tint-odd` class, overriding the alternating tint. Remove the blanket transparent
   backgrounds so `--row-tint-odd-bg` shows through (keep `[data-pair-tint]` color overrides
   if they set actual pair colours; remove only the neutral transparent ones).

3. **NavStrip P slot 1 shows 0 overnight** — `baseDayPnlForPosition` in `nav.js` hits Case 4
   (`oq > 0 && dcv === 0 && close <= 0`) and returns 0. The backend already calls
   `_override_stale_close_from_snapshot` to patch `close_price` from `daily_book`, but for
   snapshot-path positions (closed hours), `close_price` can still be 0 if `daily_book.prev_ltp`
   is absent for that row. Investigate the full data flow (backend snapshot path → frontend
   `baseDayPnlForPosition`): if `prev_ltp` is reliably populated in `daily_book`, the fix is
   to ensure it is surfaced as `close_price` in the snapshot response; if not, add a frontend
   fallback in the `close <= 0` guard (e.g. use `p?.last_price` as close proxy only when
   `pnl` is non-zero and `oq > 0`). Write the fix wherever the root cause actually is.

## Agents

- frontend: Fix all three bugs:
  **Bug 1** — `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:
  `accountChoices` (lines 1220–1233) derives from `positions` + `realAccounts`.
  `realAccounts` starts `[]` and is populated by async `loadRealAccounts()` in onMount.
  The `{#key accountChoices.length}` wrapper on the AccountMultiSelect at line 4497 forces
  a remount on length change but may reset bound state. Investigate why the dropdown still
  shows empty — is `realAccounts` empty at open time, or does the key-remount cause issues?
  Fix so the snapshot card always shows available accounts after first load.

  **Bug 2** — `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`:
  Lines 590–591: `.cand-row.expiry-band-netted { background-color: transparent; }` (and
  similar transparent rules at lines 600–617 for `[data-pair-tint]`). These override the
  global `.row-tint-odd { background: var(--row-tint-odd-bg); }` applied via the parent
  grid. Remove `background-color: transparent` from `.cand-row.expiry-band-netted` and
  `.cand-row.expiry-band-otm` base rules so alternating tint shows through. Preserve any
  `[data-pair-tint]` rules that set actual (non-transparent) pair colours.

  **Bug 3** — `frontend/src/lib/data/nav.js` + investigate backend if needed:
  `baseDayPnlForPosition(p)` at line 113: `if (close <= 0) return 0;` returns zero when
  Kite's `close_price` is 0/missing and `dcv=0`. Trace where the position row `p` comes from
  in `PositionStrip.svelte:_livePositionsToday` (lines 415-443) to understand which fields
  are available. Check if `p?.last_price` (broker LTP) is non-zero in this scenario and
  whether the fix `if (close <= 0) { const lp = Number(p?.last_price ?? 0); return lp > 0 ?
  pnl - oq * (lp - avg) : 0; }` is correct or if a different field should be used. Implement
  whichever fix is correct. If the issue is in the backend snapshot path not setting
  `close_price` from `daily_book.prev_ltp`, report the finding clearly but do NOT touch
  backend — flag for the backend-test agent.

- backend-test: Write a pytest test covering the NavStrip P slot 1 overnight scenario.
  File: `backend/tests/test_positions_navstrip_p_slot.py`.
  Test the `baseDayPnlForPosition`-equivalent logic (or the backend function that produces
  `close_price`/`prev_settlement_pnl` in the positions response) for the Case 4 scenario:
  `overnight_quantity > 0`, `day_change_val = 0`, `close_price = 0`, `prev_settlement_pnl = null`.
  Verify the result is non-zero when `pnl` and/or `last_price` reflect actual position value.
  Also add a regression test that `_override_stale_close_from_snapshot` patches `close_price=0`
  rows from `daily_book` when a valid `prev_ltp` exists. Use existing test infrastructure in
  `backend/tests/test_positions_prev_settlement_pnl.py` and `test_positions_snapshot_prev_ltp.py`
  as reference for fixtures and mock patterns.

- broker: skip
- doc: skip
- playwright: skip

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message

fix(derivatives): snapshot dropdown accounts, exp-close row tint, NavStrip P slot overnight zero

## Done when

1. Snapshot card "All accounts" dropdown shows broker account options on first render.
2. Exp-close grid shows alternating `--row-tint-odd-bg` tint on odd candidate rows.
3. NavStrip P slot 1 shows a non-zero day P&L for overnight positions when `close_price=0`
   and `day_change_val=0` (Case 4 guard fixed).
4. pytest green (including new `test_positions_navstrip_p_slot.py`).
5. svelte-check 0 errors.
