# Plan: NavStrip heartbeat + Payoff exp-profit correction

## Context

Two bugs reported during MCX-open hours:

**Bug 1 — NavStrip border animation silent during MCX-only hours**
`_heartbeatOn` $effect fires only on `_dataChangedTick` (fingerprint change — at most once per 30s
backend cache cycle). During quiet MCX periods, fingerprint rarely changes → heartbeat never
fires. The 5s `bookPollerTick` increments `_pollCycleStamp` but `_pollCycleStamp` is not tracked
in the heartbeat $effect, so the heartbeat never wakes up from book-poll cadence.

**Bug 2 — Payoff Exp P&L total wrong when showDraftInPayoff is off**
`_legsExpPnlTotal` (the number shown in the Legs panel TOTAL row and passed to the chart as
`legsExpPnlAtSpot`) does NOT apply the `showDraftInPayoff` gate. The identical filter is already
used in `legs` (line 2336-2337) to exclude provisional/draft/draft_store sources from the backend
strategy analytics call. When `showDraftInPayoff=false`, the backend payoff curve excludes drafts
(showing 406000) but `_legsExpPnlTotal` still includes them (showing 269826 — the draft closing
leg reduces the net qty from -3 to -2 lots). Same missing gate in `_expiryPnlOffset` causes the
chart's expiry offset to be wrong too.

## Agents
- frontend: Both fixes (PositionStrip.svelte + derivatives/+page.svelte)
- backend-test: skip
- broker: skip
- doc: skip
- playwright: Update navstrip_pslot_closed_hours.spec.js to assert `_pollCycleStamp` drives heartbeat; add derivatives legsExpPnlTotal spec (source-scan)

## Fix 1 — NavStrip heartbeat fires every 5s during any market-open session

**File:** `frontend/src/lib/PositionStrip.svelte`  
**Location:** `_heartbeatOn` $effect, line ~907

Add `void _pollCycleStamp;` as the first reactive read:

```javascript
// BEFORE:
$effect(() => {
  if (_dataChangedTick === 0) return;
  if (_mktTick === 0) return;
  _heartbeatOn = true;
  ...

// AFTER:
$effect(() => {
  void _pollCycleStamp;  // fire on every 5s bookPollerTick during open hours
  if (_dataChangedTick === 0) return;  // keep: skip mount (no data yet)
  if (_mktTick === 0) return;          // keep: no heartbeat during closed hours
  _heartbeatOn = true;
  ...
```

`_pollCycleStamp` already increments on every `bookPollerTick.value` change (line 144-147).
The `_mktTick === 0` gate ensures this only fires during NSE or MCX open.
The `_dataChangedTick === 0` guard prevents a mount-paint before any data has loaded.

## Fix 2 — _legsExpPnlTotal + _expiryPnlOffset respect showDraftInPayoff

**File:** `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

### _legsExpPnlTotal (line 2150-2161)

Apply the exact same source filter already used in `legs` (line 2336-2337):

```javascript
// BEFORE:
const _legsExpPnlTotal = $derived.by(() => {
  const spot = liveSpot ?? null;
  return displayedCandidates
    .filter(c => _isLegEnabled(c))
    .reduce((s, c) => {
      const v = _legExpPnlDisplay(c, spot);
      return v == null ? s : s + v;
    }, 0);
});

// AFTER:
const _legsExpPnlTotal = $derived.by(() => {
  const spot = liveSpot ?? null;
  return displayedCandidates
    .filter(c => {
      if (!_isLegEnabled(c)) return false;
      if (!showDraftInPayoff &&
          (c.source === 'provisional' || c.source === 'draft_store' || c.source === 'draft')) return false;
      return true;
    })
    .reduce((s, c) => {
      const v = _legExpPnlDisplay(c, spot);
      return v == null ? s : s + v;
    }, 0);
});
```

### _expiryPnlOffset (line 2173-2178)

Same gate — offset is passed to the chart overlay which already excludes drafts from its legs:

```javascript
// BEFORE:
const _expiryPnlOffset = $derived.by(() =>
  displayedCandidates
    .filter(c => _isLegEnabled(c) && c.kind !== 'eq')
    .reduce((s, c) => s + (Number(c.qty || 0) === 0
      ? Number(c.realised || c.pnl || 0)
      : Number(c.realised || 0)), 0)
);

// AFTER:
const _expiryPnlOffset = $derived.by(() =>
  displayedCandidates
    .filter(c => {
      if (!_isLegEnabled(c) || c.kind === 'eq') return false;
      if (!showDraftInPayoff &&
          (c.source === 'provisional' || c.source === 'draft_store' || c.source === 'draft')) return false;
      return true;
    })
    .reduce((s, c) => s + (Number(c.qty || 0) === 0
      ? Number(c.realised || c.pnl || 0)
      : Number(c.realised || 0)), 0)
);
```

**Why both:** `_legsExpPnlTotal` is the number in the Legs TOTAL row + `legsExpPnlAtSpot` prop on
the chart. `_expiryPnlOffset` shifts the expiry curve. Both must use the same source set as `legs`
or the chart and the TOTAL row diverge.

**Behaviour when `showDraftInPayoff=true` (default):** unchanged — all sources included.  
**Behaviour when `showDraftInPayoff=false`:** drafts/provisional excluded from TOTAL row and
offset, matching what the backend payoff curve already computed.

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message
fix(derivatives+navstrip): _legsExpPnlTotal respects showDraftInPayoff + heartbeat fires on bookPollerTick

## Done when
1. MCX open: navstrip amber border animation fires every ~5s (not only on data change)
2. With showDraftInPayoff=false: Legs TOTAL Exp P&L = same as backend payoff at current spot
3. With showDraftInPayoff=true: no behaviour change (all sources included as before)
4. svelte-check 0 errors, playwright spec passes
