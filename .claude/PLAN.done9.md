# Plan: Audit + remove post-store derivation masking across all surfaces

## Context
The pattern "fetch from store → if null/zero, compute/fallback to something else" silently
masks underlying issues: broken WebSocket subscriptions, unresolved tokens, missing
instrument data. When a value is unavailable, the surface should show blank/null so the gap
is visible and fixable at the source. Derivation on top of store values is only valid for
business formulas (day P&L = (ltp − close) × qty) and display transforms, NOT for filling
in missing live-market data from stale backend polls.

## Known surfaces to fix

### 1 — `liveSpot` Tiers 3/4/5 in derivatives page
File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` (~line 1785)

**Remove** Tiers 3, 4, 5 entirely:
- Tier 3: `candidatePositions[*].underlying_ltp` — backend-polled, stale
- Tier 4: `strategy?.spot` — backend 5s poll, stale
- Tier 5: `getSnapshot(selectedUnderlying)?.ltp` — fallback on bare root "GOLDM" which
  never matches a tick (root key ≠ full tradingsymbol); effectively always stale

**Keep** only Tier 1a (anchor contract SSE), Tier 1b (strategy underlying SSE), Tier 2
(resolved front-month SSE). If all three miss → return `undefined`. Chart shows blank spot
marker, making the subscription gap visible.

The `undefined` return will also surface whether `spot_anchor_contract` is being set to
the correct full tradingsymbol (e.g. `GOLDM25NOVFUT`) vs a root alias.

### 2 — `mkResolveCellLtp` fallback to `p.data.ltp`
File: `frontend/src/lib/data/pulseColumns.js` (~line 103–124)

Currently: `return snap[quoteSym || sym] ?? p.data.ltp`

`p.data.ltp` is the broker-polled LTP from buildUnified (up to 10s old). If symbolStore
has no tick for this symbol, showing a stale broker value is not masking in the same way —
it IS the last known price from the broker API. **Justify in comment**: fallback to
`p.data.ltp` is valid because it represents the broker's last-reported price before any
SSE tick; it is NOT a derived value, just the seed value before WebSocket overrides it.
No code change needed here — add a one-line comment explaining why the fallback is correct.

### 3 — `cur_val` valueGetter fallback to `p.data.cur_val`
File: `frontend/src/lib/data/pulseColumns.js` (~line 620)

Currently falls back to `p.data.cur_val` when `ltp === 0`. `p.data.cur_val` was computed
at buildUnified time (10s stale). This masks a zero-ltp condition.

**Fix**: when `ltp === 0` or not in snap, return `null` (show blank) instead of
`p.data.cur_val`. If symbolStore has no live tick, the Value column should be blank — not
a stale approximation. The total-row aggregation will also show blank when live values are
missing, surfacing the gap.

**Exception**: if `ltp_ts === 0` AND `p.data.ltp > 0` — broker poll seeded the value but
no SSE tick yet. In this case `p.data.cur_val` is valid. Check via `ltp_ts` if accessible,
otherwise rely on the snap value being the broker seed.

Actually — simpler: `mkResolveCellLtp` already handles this correctly (falls back to
`p.data.ltp` which is the broker-seeded value). Use the same resolved LTP for `cur_val`:

```javascript
valueGetter: (p) => {
  if (!p.data || p.data._isTotal) return p.data?.cur_val ?? null;
  const heldAbs = Math.abs(Number(p.data.qty_hold) || 0);
  if (heldAbs === 0) return null;
  const ltp = resolveCellLtp({ data: p.data });  // reuse same resolver as LTP column
  return (ltp > 0) ? ltp * heldAbs : null;       // null (not p.data.cur_val) when ltp=0
},
```

### 4 — `day_pnl` valueGetter fallback `?? p.data?.day_pnl`
File: `frontend/src/lib/MarketPulse.svelte` (~line 3615)

Currently: `return positionsDerivedStore.get(sym).day_pnl ?? p.data?.day_pnl`

`p.data?.day_pnl` is a backend-computed value from the broker API. `positionsDerivedStore`
recomputes it at 4Hz from symbolStore. If the store-derived value is null (symbolStore
has no tick), the fallback to `p.data?.day_pnl` is the broker's own day_change_val — which
IS a valid value (Kite computes it), not a silent approximation.

**Justify in comment**: `p.data?.day_pnl` fallback is the broker-reported day_change_val
from the last poll, not a derived approximation. Valid when symbolStore tick hasn't arrived
yet. No code change — add comment.

### 5 — `buildUnified` spot → `row.ltp` in `pulseUnified.js`
File: `frontend/src/lib/data/pulseUnified.js` (~line 440–494)

Check if `snapOf(sym)?.ltp` for positions/holdings falls back to `row.last_price` or
`row.ltp` from the broker API when symbolStore has no entry. Same justification as
`mkResolveCellLtp` — the broker-polled ltp IS the last known value. Confirm and comment.

## Agents
- backend: skip
- frontend: Implement fixes 1 and 3 above. Add justifying comments for 2 and 4.
  For fix 1: in `liveSpot` $derived, remove Tiers 3, 4, 5. The terminal case becomes
  `return undefined` (not `strategy?.spot`).
  For fix 3: in `cur_val` valueGetter, change `p.data.cur_val` fallback to `null`.
  For 2/4: add one-line comment at each fallback explaining it is the broker-seed value,
  not a stale derivation.
  Also audit `pulseUnified.js` buildUnified for any other post-store derivation that lacks
  a justifying comment; add comments or fix as needed.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(payoff/pulse): remove stale fallback tiers from liveSpot; cur_val returns null when ltp=0

## Done when
- `liveSpot` $derived has only Tiers 1a, 1b, 2 (all symbolStore SSE reads); returns
  `undefined` when all miss — payoff spot marker is blank when subscription gap exists
- `cur_val` valueGetter returns `null` (not `p.data.cur_val`) when live ltp = 0
- `mkResolveCellLtp` and `day_pnl` fallbacks have one-line justification comments
- svelte-check 0 errors
