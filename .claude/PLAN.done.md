# Plan: Fix liveSpot stale-strategy leak + conn grid density

## Task

**Bug 1: Equity option payoff parallel lines — previous fix incomplete**

The tier-4 guard added in the last commit (check `stratUnd === selectedUnderlying` before returning `strategy.spot`) never fires because tiers 1 and 2 leak the stale value first:

- Tier 1: reads `strategy?.spot_anchor_contract` (e.g. "NIFTY25JULFUT") unconditionally → `getSnapshot("NIFTY25JULFUT")?.ltp = 25000` → returns immediately
- Tier 2: reads `strategy?.underlying` (e.g. "NIFTY") unconditionally → `getSnapshot("NIFTY")?.ltp = 25000` → returns immediately

Both tiers 1 and 2 must be gated on `strategy.underlying === selectedUnderlying`. The correct fix restructures `liveSpot` so that strategy-derived SSE lookups (anchor + underlying) only run when the cached strategy is for the SAME underlying.

**Fix**: Rewrite the full `liveSpot` block (lines 1726–1759 approx):

```javascript
const liveSpot = $derived.by(() => {
  void _throttledTick;
  const stratUnd = String(strategy?.underlying || '').toUpperCase();
  const stratMatchesSel = stratUnd && stratUnd === selectedUnderlying;

  if (stratMatchesSel) {
    const anchor = String(strategy?.spot_anchor_contract || '').toUpperCase();
    if (anchor) {
      const v = Number(untrack(() => getSnapshot(anchor)?.ltp));
      if (Number.isFinite(v) && v > 0) return v;
    }
    const v = Number(untrack(() => getSnapshot(stratUnd)?.ltp));
    if (Number.isFinite(v) && v > 0) return v;
  }

  // Tier 3: batchQuote for selectedUnderlying (30 s stale at most).
  // untrack() is essential — _underlyingQuotes is replaced wholesale every
  // 30 s; tracking it would re-derive liveSpot on every poll, defeating
  // the 250 ms _throttledTick gate and causing extra SVG re-renders.
  const bqLtp = untrack(() => _underlyingQuotes[selectedUnderlying]?.ltp);
  if (bqLtp != null && Number.isFinite(bqLtp) && bqLtp > 0) return bqLtp;

  // Tier 4: server-side spot — only when strategy is for the same underlying.
  return stratMatchesSel ? strategy?.spot : undefined;
});
```

**Bug 2: Conn grid density**

`.lp-conn-row` has `font-size: var(--fs-base, 0.78rem)` and `gap: 0.5rem`. 
- System rows use `font-size: 0.72rem` — the larger font makes conn rows taller than system rows and the column content bigger than necessary
- `gap: 0.5rem` creates excess horizontal space between account / broker / type columns

Fix: change `.lp-conn-row` in `frontend/src/lib/LogPanel.svelte`:
- `font-size: var(--fs-base, 0.78rem)` → `font-size: 0.72rem`
- `gap: 0.5rem` → `gap: 0.25rem`

## Agents

- backend: skip
- frontend: Two fixes. (1) In `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`, find the `liveSpot` derived block (around line 1726). Replace the ENTIRE block body with the restructured version that gates tiers 1 and 2 (anchor + underlying SSE lookups) behind `stratMatchesSel = stratUnd && stratUnd === selectedUnderlying`. Preserve the existing long comment block explaining tier 3 (`_underlyingQuotes` / `untrack()` rationale). Keep the opening `void _throttledTick;` line. The logic: compute `stratUnd` and `stratMatchesSel` first; if stratMatchesSel, try anchor then stratUnd via getSnapshot; then try batchQuote (tier 3, always, with untrack); finally return `stratMatchesSel ? strategy?.spot : undefined`. Do NOT touch anything outside the liveSpot block. (2) In `frontend/src/lib/LogPanel.svelte`, find `.lp-conn-row` CSS (around line 2354): change `font-size: var(--fs-base, 0.78rem)` → `font-size: 0.72rem` and `gap: 0.5rem` → `gap: 0.25rem`.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

fix(derivatives): gate liveSpot tiers 1+2 on underlying match; tighten conn row density

## Done when

- Switching BHEL → NIFTY → BHEL shows correct option payoff (not parallel lines)
- Conn grid rows have same visual height as system tab rows; account/broker columns have tighter spacing
- svelte-check exits 0
