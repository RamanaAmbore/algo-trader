# Plan: Show signed lots for short positions (remove Math.abs from display)

## Task
Short positions have negative qty in DB/API. `lotsForRow.js` applies `Math.abs()` before
computing lot count, so shorts display as positive (e.g. `2L` instead of `-2L`). The fix
removes `Math.abs()` from the display path so lot sign is preserved end-to-end.

## Agents
- backend: skip
- frontend: In `frontend/src/lib/data/lotsForRow.js`:
    1. Line 37 — remove `Math.abs` from `qPos`: `Number(qPosRaw) || 0`
    2. Line 38 — remove `Math.abs` from `qHold`: `Number(qHoldRaw) || 0`
    3. Line 65 — remove `Math.abs` from fast path: `return Number(row.lots) || 0;`
    4. Line 67 — change `qPos > 0` to `qPos !== 0` so signed divide works
  In `frontend/src/lib/MarketPulse.svelte` line 3237:
    5. Change `_pLots > 0` to `_pLots !== 0` so the `L` label renders for short positions too
  `fmtLots()` already handles negatives correctly — no change needed there.
  `PositionStrip.svelte:616` and `MarketPulse.svelte:2476` use `Math.abs` for *financial*
  math (longOptionsCashPaid), already guarded against negatives — do NOT change those.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(pulse): show signed lots for short positions — remove Math.abs from lotsForRow display path

## Done when
`lotsForRow({ lots: -2, ... })` returns `-2`; short position P-badge shows `-2L`; Vitest
passes; svelte-check 0 errors. `frontend/src/lib/__tests__/data/lotsForRow.test.js` updated:
- line 100-105 test description updated, expectation changed from 2 → -2
- new test added: short via fallback path (no `lots` field, negative qty_pos / lot)
- new fmtLots tests: `-2` → `"-2"`, `-1.5` → `"-1.5"`
