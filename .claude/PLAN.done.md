# Plan: fix LTP/chg% flash visibility + shrink St column

## Task
Three small targeted edits in `pulseColumns.js`:

1. **`st` column** (`mkRightColDefs` line 580): reduce `width/minWidth/maxWidth` from 38 to 28.
2. **LTP flash fix** (`_ltpCellClass` lines 309-313): `mp-pnl-cell.cell-pos/neg/flat` uses
   `background-color: !important` (MarketPulse.svelte:4756-4758), which overrides the CSS
   animation on `tf-up`/`tf-down`. Fix: move the `dirBg + 'mp-pnl-cell'` push so it only
   fires when NOT currently flashing. Compute `dayPct`/`dirBg` before the `_isAnimating`
   branch; skip `mp-pnl-cell` when `fc !== null`, restore it in the else branch and in
   the snapshot path.
3. **chg% (mkPnlCellClass) flash fix** (lines 102-113): same root cause — `inFlashUp`/
   `inFlashDown` paths return `` `${base} ${_bgFlashClass(...)}` `` where `base` already
   includes `mp-pnl-cell`. Remove `mp-pnl-cell` from the flash return paths only:
   return `` `${RA} ${dirCls(p.value)} ${_bgFlashClass(...)}` `` instead of
   `` `${base} ${_bgFlashClass(...)}` `` when `inFlashUp`/`inFlashDown`.
   Non-flash return (poll-diff and base) keeps `mp-pnl-cell`.

After either flash clears (300 ms timer fires → `_scheduleFlashRefresh` → `refreshCells`),
the cellClass re-evaluates without the flash sym in the Set, restoring `mp-pnl-cell`.

## Agents
- backend: skip
- frontend: edit `frontend/src/lib/data/pulseColumns.js` — three changes above
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- vitest: yes (`pulseColumns.test.js` covers column ordering; no new assertions needed for
  CSS logic, but run vitest to confirm no regressions)

## Commit message
fix(pulse): restore LTP + chg% flash — drop mp-pnl-cell during active tf-up/tf-down animation; shrink St column to 28px

## Done when
- `st` column width is 28px
- LTP cells flash visibly on tick (tf-up/tf-down animation runs without !important override)
- chg% cells (day_pnl_pct, left_change_pct) flash visibly on tick
- `npx svelte-check` reports 0 errors
- `npx vitest run` passes with 0 failures
