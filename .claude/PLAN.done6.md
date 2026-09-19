# Plan: Legs chg% SSOT — use same formula as positions pulse

## Context
Legs grid shows chg% = 0/null for all candidates. Positions pulse shows correct chg%.
Root cause: `buildPositionRowFromBroker()` in `pageLoad.js` maps `day_change_val` but
NOT `day_pnl`. So `c.day_pnl` is `undefined` for every leg candidate. The `_chgPct`
derived in `CandidateLegRow.svelte` then falls to `(ltp − prev_close) / prev_close`,
which also returns null because `prev_close` is null (Kite sends `close_price = 0` for
many F&O strikes; `previous_close` is not in `PositionRow` schema).

Positions pulse computes chg% as `day_pnl / (close_price × qty) × 100` via
`_dayPnlPctValueGetter` in `pulseColumns.js`. Legs should use the same primary formula,
with `(ltp − prev_close) / prev_close` as a live-tick secondary once `prev_close` is
known (for overnight F&O with valid close_price from `_override_stale_close_from_snapshot`).

## Agents
- backend: skip
- frontend: All changes below
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Frontend agent task

### Change 1 — Map `day_pnl` in buildPositionRowFromBroker

**File: `frontend/src/lib/derivatives/pageLoad.js`**

In `buildPositionRowFromBroker()` (around line 61), add `day_pnl` to the returned object,
alongside the existing `day_change_val` mapping:

```js
day_pnl: p?.day_pnl != null ? Number(p.day_pnl) : null,
```

### Change 2 — Update `_chgPct` formula in CandidateLegRow.svelte

**File: `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`**

Find the existing `_chgPct` derived (around line 133):
```js
const _chgPct = $derived(
  c.change_pct != null ? c.change_pct :
  (typeof ltp === 'number' && typeof c.prev_close === 'number' && c.prev_close > 0
    ? (ltp - c.prev_close) / c.prev_close * 100 : null)
);
```

Replace with the same primary formula used by positions pulse
(`day_pnl / (prev_close × qty)`), keeping live-tick as secondary:
```js
const _chgPct = $derived(() => {
  // Primary: same formula as positions pulse _dayPnlPctValueGetter
  const pc = c.prev_close ?? 0;
  const prevMv = pc > 0 ? pc * Math.abs(c.qty || 0) : 0;
  if (c.day_pnl != null && prevMv > 0) return (c.day_pnl / prevMv) * 100;
  // Secondary: live update per tick (works when prev_close is available)
  if (typeof ltp === 'number' && pc > 0) return (ltp - pc) / pc * 100;
  return null;
});
```

---

After edits, run:
```
cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1
```
Fix any errors. Report what changed.

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(derivatives): legs chg% SSOT — use same day_pnl/(prev_close×qty) formula as positions pulse

## Done when
- `buildPositionRowFromBroker()` maps `day_pnl` from position row
- `_chgPct` in legs uses `day_pnl / (prev_close × qty)` as primary (matching pulse)
- Legs chg% shows non-zero values matching positions pulse for the same symbols
- svelte-check 0 errors
