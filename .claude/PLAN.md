# Plan: Expiry P&L SSOT + Closed-Hours Broker Refresh

## Task

Two connected issues:

**1. CRUDEOIL overlay EXP ≠ payoff/tooltip EXP (wrong SSOT)**

Root cause: `chartPnlOffset` (= `candidatesActualPnl − chartTheoreticalAtSpot`) is applied to BOTH
`today_value` AND `expiry_value` inside `OptionsPayoff.adjustedPayoff`. The offset is designed to
align the TODAY (TDAY) curve with broker MTM P&L — but it leaks into the EXPIRY curve. Since
`chartPnlOffset` includes the BS-vs-broker drift (not just realised P&L), the EXPIRY value shown
in the tooltip (`adjustedPayoff.expiry_value`) can diverge dramatically from the stat overlay EXP
(`_legsExpPnlTotal` = `expiryPnl()` sum). User sees two different "EXP at current spot" numbers.
SSOT for expiry P&L is `expiryPnl()` in `frontend/src/lib/data/expiryPnl.js`.

Fix: split the offset. Apply `chartPnlOffset` only to `today_value`; apply only the REALISED P&L
component (`Σ c.realised for enabled F&O candidates`) to `expiry_value`. After the fix:
- Overlay EXP = `Σ (expiryPnl(c, spot) + c.realised)` = pure theoretical expiry + locked-in gains
- Tooltip EXP at current spot = backend theoretical expiry + `Σ c.realised` ≈ same ✓
- Legs grid EXP column already uses `expiryPnl()` ✓

**2. Positions/holdings/funds not refreshed when market is closed**

Currently the `closed_hours_or_broker()` gate in `snapshot_gate.py` blocks ALL broker calls during
closed hours and returns the last intraday snapshot. Post-settlement, broker systems update position
close prices, realised P&L, and fund values — but the snapshot is never re-fetched.

Fix: add a low-frequency background poller (every 30 min during closed hours) that fetches
positions/holdings/funds/margins from broker and updates `daily_book`. The route gate already
returns the snapshot when closed; we just need the snapshot to be refreshed post-settlement.

---

## Root Cause Detail

### Expiry P&L divergence (primary)

`OptionsPayoff.svelte` lines ~144-151:
```js
const adjustedPayoff = $derived.by(() => {
  // ...
  return payoff.map(p => ({
    spot:         p.spot,
    today_value:  p.today_value + realizedPnl,    // ← correct: aligns TDAY to broker P&L
    expiry_value: p.expiry_value + realizedPnl,   // ← BUG: chartPnlOffset ≠ just realised P&L
  }));
});
```

`realizedPnl` here = `chartPnlOffset` = `candidatesActualPnl − chartTheoreticalAtSpot`.

`chartPnlOffset` = realised P&L (closed legs) + BS-vs-broker drift (open legs).

Adding the BS-vs-broker drift to the EXPIRY curve makes no sense: expiry P&L is intrinsic only,
independent of time-value or BS pricing.

### Closed-hours data staleness (secondary)

`background.py` background poller runs `_fetch_positions_direct()` only when `_any_segment_open()`.
During closed hours, the poller sleeps. After settlement (5-15 min after MCX close, 30-45 min
after NSE/BSE close), broker data updates with final settled values. No task re-fetches them.

---

## Agents

- frontend: Fix `adjustedPayoff` in `OptionsPayoff.svelte`: accept new `expiryPnlOffset` prop; apply `realizedPnl` only to `today_value`, `expiryPnlOffset` to `expiry_value`. In `derivatives/+page.svelte`: compute `_expiryPnlOffset = Σ c.realised for enabled F&O displayedCandidates` and pass as `expiryPnlOffset` to OptionsPayoff (keep existing `chartPnlOffset` as `realizedPnl` for today-curve unchanged).
- backend: Add a closed-hours refresh task in `background.py`: after market close, poll broker for positions/holdings/funds every 30 min and write to `daily_book` (call the existing snapshot-write path). Use `_any_segment_open()` to guard — only runs when closed. Stop on next open. Wire into the existing background task start/stop machinery.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(derivatives): expiry P&L SSOT — chartPnlOffset applied to today-curve only; expiryPnlOffset for realised; closed-hours broker refresh every 30 min

## Done when
- CRUDEOIL overlay EXP matches tooltip EXP at the same spot (within rounding).
- Changing the chartPnlOffset (by toggling a leg) changes TDAY but NOT EXP in the tooltip.
- After market close, positions/holdings/funds are re-fetched from broker every 30 min.
- svelte-check 0 errors.

---

## Frontend agent brief

### File 1: `frontend/src/lib/OptionsPayoff.svelte`

Read the file around line 75-155 (props + `adjustedPayoff` derived).

**Step 1** — Add new prop `expiryPnlOffset`:
```js
expiryPnlOffset = /** @type {number} */ (0),
```
(Add next to the existing `legsExpPnlAtSpot` prop.)

**Step 2** — Change `adjustedPayoff` to use separate offsets:
```js
const adjustedPayoff = $derived.by(() => {
  if (!payoff.length) return payoff;
  const todayOff  = realizedPnl || 0;
  const expiryOff = expiryPnlOffset || 0;
  if (!todayOff && !expiryOff) return payoff;
  return payoff.map(p => ({
    spot:         p.spot,
    today_value:  p.today_value != null ? p.today_value + todayOff : null,
    expiry_value: p.expiry_value + expiryOff,   // only realised P&L, not BS drift
  }));
});
```

Make sure `adjustedBreakevens` (if it exists) also uses `adjustedPayoff` (it should already — check).

### File 2: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

**Step 1** — Add `_expiryPnlOffset` derived (near `_legsExpPnlTotal`, around line 1897):
```js
/** Realised P&L offset for the expiry curve — locked-in gains from
 *  partially/fully closed F&O legs. Adds to backend expiry_value so
 *  tooltip matches overlay EXP (which adds c.realised via _legExpPnlDisplay). */
const _expiryPnlOffset = $derived.by(() =>
  displayedCandidates
    .filter(c => _isLegEnabled(c) && c.kind !== 'eq')
    .reduce((s, c) => s + Number(c.realised || 0), 0)
);
```

**Step 2** — Pass `expiryPnlOffset` to `<OptionsPayoff>` (around line 4117 where `realizedPnl` is passed):
```svelte
expiryPnlOffset={_expiryPnlOffset}
```

After changes, run:
```
cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1 | tail -10
```
to verify 0 new errors.

---

## Backend agent brief

### File: `backend/api/background.py`

**Goal**: After market close, refresh positions/holdings/funds from broker every 30 min and
write to the `daily_book` snapshot so the closed-hours route returns fresh data.

**Step 1** — Read the file to understand the background task structure. Find:
- `_fetch_positions_direct()` or equivalent live-data fetch
- How `_any_segment_open()` / market-open check is used
- Where the daily_snapshot writer is called (look for `daily_snapshot.write_positions` or similar)
- The asyncio task lifecycle

**Step 2** — Add a `_closed_hours_refresh_loop()` async coroutine that:
- Checks `not _any_segment_open()` (only run when market is closed)
- Calls `daily_snapshot.write_positions_snapshot()` (or equivalent) to fetch from broker and persist
- Does the same for holdings and funds
- Sleeps 1800 seconds (30 min)
- Loops until market re-opens (when open: exits so the regular poller takes over)

**Step 3** — Wire into the existing task start/stop machinery so it starts when the market closes
and stops when it opens. Mirror the pattern used for other background tasks.

**Important**: Do NOT call broker APIs synchronously. Use the existing async broker wrappers.
Do NOT re-implement the snapshot write logic — reuse the existing `daily_snapshot` module functions.
If the existing snapshot write functions don't accept a "force" param, use `_raw_cache_invalidate()`
before calling them so the broker is actually polled (not served from the 30s raw cache).
