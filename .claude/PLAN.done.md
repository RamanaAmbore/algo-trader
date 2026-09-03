# Plan: Fix payoff overlay vs snapshot card spot price SSOT drift

## Context
The payoff overlay (top-left chip in OptionsPayoff) shows a spot price that diverges from
what the Snapshot card shows for the same underlying (e.g. 155450 vs 151953, ~3500pt gap).

Root cause — two different price sources reading different contracts:
- **Payoff `liveSpot`**: reads `getSnapshot(strategy.spot_anchor_contract)` via SSE ticks
  (Tier 1, lines 1905-1912). `spot_anchor_contract` is the contract the loaded strategy is
  anchored to — could be any expiry month.
- **Snapshot card `_underlyingQuotes[root].ltp`**: built from `loadUnderlyingQuotes()` which
  calls `resolveUnderlying(g.underlying, findNearestFuture)` for the quoteKey — always the
  NEAREST front-month futures contract.

When `spot_anchor_contract` is a far-month future and `resolveUnderlying` returns a nearer
contract, `liveSpot` and `_underlyingQuotes[root].ltp` refer to two completely different
instruments — hence the 3500pt spread.

Additionally, when anchor-contract ticks arrive in `tickBus`, the `flash.update` for
`${root}:ltp` is never triggered (because `sym` is the anchor tradingsymbol, not `root`),
so the LTP cell in the snapshot card doesn't flash on real-time price moves.

## Fix
Single file: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

### Change 1 — Snapshot card: use `liveSpot` as SSOT for selected underlying (lines 4810-4813)

Replace:
```svelte
{@const _q = _underlyingQuotes[g.underlying]}
{@const _ltp  = _q ? Number(_q.ltp) : null}
{@const _close = _q ? Number(_q.prev_close) : null}
{@const _pct  = _q && _q.day_pct != null ? Number(_q.day_pct) : null}
```

With:
```svelte
{@const _q = _underlyingQuotes[g.underlying]}
{@const _useAnchor = g.underlying === selectedUnderlying && liveSpot != null && liveSpot > 0}
{@const _ltp   = _useAnchor ? liveSpot : (_q ? Number(_q.ltp) : null)}
{@const _close = _useAnchor && (strategy?.spot_prev_close ?? 0) > 0
    ? Number(strategy.spot_prev_close)
    : (_q ? Number(_q.prev_close) : null)}
{@const _pct   = _ltp != null && _close != null && _close > 0
    ? ((_ltp - _close) / _close) * 100
    : (_q?.day_pct ?? null)}
```

- For the selected underlying: LTP = `liveSpot` (anchor contract SSE tick), prev_close =
  `strategy.spot_prev_close` (anchor's settlement), day% = recomputed from these.
- For all other underlyings: unchanged (still use batch quote).

### Change 2 — tickBus: flash the underlying's LTP cell when anchor ticks (lines 1840-1851)

After the existing `if (root in _underlyingQuotes) { ... }` block, add:

```javascript
// Anchor contract tick → flash the underlying's spot cell in the snapshot card.
// Without this the LTP cell never flashes when spot_anchor_contract is a far-month
// future (its tradingsymbol ≠ root key) even though liveSpot is updating.
const _anchor = String(strategy?.spot_anchor_contract || '').toUpperCase();
const _stratUnd = String(strategy?.underlying || '').toUpperCase();
if (_anchor && root === _anchor && _stratUnd && _stratUnd in _underlyingQuotes) {
  const _as = getSnapshot(root);
  if (_as?.ltp != null) flash.update(`${_stratUnd}:ltp`, Number(_as.ltp));
}
```

## Agents
- frontend: Implement both changes in +page.svelte as described above. Add a brief code
  comment on the `_useAnchor` line explaining the SSOT motivation (anchor vs near-month
  divergence). No other files need changes.
- playwright: Add a new spec `e2e/derivatives_spot_ssot.spec.js` that:
  1. Loads a strategy with a known underlying
  2. Checks that the spot price shown in the Snapshot card for the selected underlying
     matches the spot shown in the OptionsPayoff overlay header chip
  3. Asserts the two values are equal (within 1 rupee tolerance to allow for tick timing)
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message
fix(derivatives): sync snapshot card spot price with payoff overlay — use liveSpot (anchor contract) as SSOT for selected underlying

## Done when
- Snapshot card LTP for selected underlying = payoff overlay spot (same price, same contract)
- Day % in snapshot card recomputed from anchor contract's prev_close (via strategy.spot_prev_close)
- LTP cell flashes in snapshot when anchor contract ticks via SSE
- svelte-check 0 errors, playwright spec passes
