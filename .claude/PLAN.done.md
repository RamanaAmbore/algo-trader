# Plan: Fix Derivatives Exp P&L, Day P&L sync, and column sequence

## Context
Three regressions / gaps observed after the derivatives SSOT refactor (fd408b1a):

1. **GOLDM Exp P&L shows a loss** (~-X) instead of expected ~2.82L profit — a regression in `rawPosExpPnl` introduced in our last commit.
2. **Day P&L not in sync** across Pulse, Legs, Snapshot, NavStrip — `positionsDerivedStore.byKey[sym]` is last-write-wins (not accumulated) for multi-account same-symbol positions.
3. **Column sequence mismatch** — Legs has Acct between P&L and Exp P&L; MarketPulse doesn't show Exp P&L / Extrinsic at all. User wants Day P&L → P&L → Exp P&L → Extrinsic in sequence across all three grids.

---

## Task
Fix the three issues in one commit.

---

## Agents

- **frontend**: Fix all three issues as detailed below. Skip backend and broker.
- **backend**: skip
- **broker**: skip
- **doc**: skip
- **backend-test**: skip
- **frontend-test**: Update `derivativesMath.test.js` — fix `rawPosExpPnl` futures tests to expect `spot`-based result (not `last_price`).
- **playwright**: skip

---

## Fix 1: rawPosExpPnl futures branch (derivativesMath.js)

**File:** `frontend/src/lib/data/derivativesMath.js`

**Bug:** The `fut` branch ignores the `spot` parameter passed in (which is the resolved underlying LTP via 4-tier chain) and reads stale `c.last_price` from the position row instead:
```javascript
// BUGGY
if (c.kind === 'fut') {
  const live = Number(c.last_price ?? 0);
  return live > 0 ? (live - avg) * qty + realised : null;
}
```

**Fix:** Use `spot` when available; fall back to `last_price` only when spot is unavailable:
```javascript
// FIXED
if (c.kind === 'fut') {
  const ref = (spot != null && spot > 0) ? spot : Number(c.last_price ?? 0);
  return ref > 0 ? (ref - avg) * qty + realised : null;
}
```

This aligns futures with the same spot resolution used for options (4-tier: batch-quote → SSE tick → positions underlying_ltp → strategy.spot).

---

## Fix 2: positionsDerivedStore byKey accumulation (positionsDerivedStore.svelte.js)

**File:** `frontend/src/lib/data/positionsDerivedStore.svelte.js`

**Bug:** `byKey[sym] = { day_pnl, exp_pnl, extrinsic, pnl }` is last-write-wins — for the same symbol across two accounts only the last position row survives.

**Fix:** Accumulate like `byRootPositions` already does:
```javascript
// Replace last-write-wins assignment with accumulation:
if (!byKey[sym]) byKey[sym] = { day_pnl: 0, exp_pnl: null, extrinsic: null, pnl: 0 };
const bk = byKey[sym];
bk.day_pnl += day_pnl;
bk.pnl     += pnl;
if (exp_pnl   != null) bk.exp_pnl   = (bk.exp_pnl   ?? 0) + exp_pnl;
if (extrinsic != null) bk.extrinsic = (bk.extrinsic ?? 0) + extrinsic;
```

This makes MarketPulse per-symbol Day P&L and NavStrip (which uses `total.day_pnl`, already a direct sum, so already correct) agree on the accumulated value.

---

## Fix 3: Column sequence — Legs and Pulse

### Legs panel (derivatives/+page.svelte ~line 4540)

Current order: ... Day P&L, P&L, **Acct**, Exp P&L, Extrinsic, IV, Greeks ...

Move `<th>Acct</th>` and corresponding `<td>` cells to after Extrinsic:
... Day P&L, P&L, Exp P&L, Extrinsic, **Acct**, IV, Greeks ...

### MarketPulse Right grid (MarketPulse.svelte + pulseColumns.js)

`mkExpPnlCol()` and `mkExtrinsicCol()` already exist in `pulseColumns.js` (lines 635–677) but are not wired into the grid. Insert them into the `mkRightColDefs()` return array right after P&L (current position 11):

```javascript
// In mkRightColDefs(), after pnlCol (line 531), before pnlPctCol:
mkExpPnlCol(),
mkExtrinsicCol(),
```

Result sequence: ..., Day P&L, Day%, P&L, **Exp P&L, Extrinsic**, P&L%, P&L/sh, ...

(Snapshot is already correct: Day P&L → P&L → Exp P&L → Extrinsic.)

---

## Tests
- **pytest**: no
- **svelte-check**: yes
- **playwright**: no

## Commit message
fix(derivatives): rawPosExpPnl futures → use spot not last_price; fix byKey accumulation; align column sequence Legs+Pulse

## Done when
1. GOLDM Snapshot Exp P&L row shows a profit value consistent with `(liveSpot − avg) × qty` (not stale last_price)
2. MarketPulse per-symbol Day P&L matches sum across all accounts for that symbol (no last-write-wins drop)
3. Legs grid: Day P&L → P&L → Exp P&L → Extrinsic → Acct (contiguous block)
4. MarketPulse right grid: Day P&L → P&L → Exp P&L → Extrinsic visible in sequence
5. svelte-check: 0 errors; Vitest passes
