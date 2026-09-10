# Plan: Derivatives data-sync overhaul — unify reactive chain for positions, quotes, payoff, snapshot

## Context

The derivatives page maintains a local `positions` $state (line 3415) that is a manually-managed
copy of `positionsStore.value`. Every derived surface (per-row P&L, Exp P&L, `_byUnderlyingTotals`,
`candidatePositions`, legs, payoff) reads from this local copy. The book poller in the layout
refreshes `positionsStore.value` every 5s, but nothing propagates that update into local `positions`.
A comment at line 4028-4030 confirms the periodic `loadPositions()` poll was deliberately removed
("Fix 7: timed positions poll removed — book poller keeps positionsStore fresh") — but the
consequence is that local `positions` only updates on fills/WS events, creating a dual-source
architecture where TOTAL row ← live, per-row ← stale.

---

## Confirmed Issues (code-verified)

### Issue 1 — P1: `loadStrategy()` clears cached strategy before instruments ready (line 3753)

**Root cause:** `_loadCache()` restores `strategy` from sessionStorage. Before `await loadInstruments()`
returns, Svelte flushes all pending effects with `instrumentsReady = false`. This causes
`candidatePositions = []` (MCX positions dropped at line 314 of pageLoad.js when `getInstrument`
returns null) → `legs = []` → legs-change effect fires `loadStrategy()` → `buildCleanLegs` returns `[]`
→ hits this line:
```js
// line 3753
if (!_hasEnabledLegs && strategy !== null) strategy = null;  // wipes sessionStorage-restored strategy
```
The cached strategy is gone. Payoff chart goes blank until a new API response arrives.

**Fix:** Guard with `&& _positionsLoaded && instrumentsReady`:
```js
if (!_hasEnabledLegs && strategy !== null && _positionsLoaded && instrumentsReady) strategy = null;
```

---

### Issue 2 — P1: `loadUnderlyingQuotes()` always no-ops on first call (line 1153-1155)

**Root cause:** onMount sequence:
```
loadPositions({ fresh: true });   // async, no await — positions not loaded yet
await loadInstruments();          // Svelte effect flush happens here
instrumentsReady = true;
loadUnderlyingQuotes();           // ← called here: positions = [] → _underlyingQuoteKeys = [] → early return
```
`_underlyingQuoteKeys` derives from `_byUnderlyingTotals` → `positions`. Since `positions` is still `[]`,
`_underlyingQuoteKeys` is `[]` → `loadUnderlyingQuotes()` returns immediately (line 1155: `if (pairs.length === 0) return`).
Result: `_underlyingQuotes['CRUDEOIL']` stays `{}` → `liveSpot` = 0 → `_clientPayoffStub` returns `[]` → chart blank.

**Fix:** Call `loadUnderlyingQuotes()` fire-and-forget at the end of `loadPositions()`, after `positions = merged`
(~line 3666). This guarantees that each time positions are freshly loaded, quotes are re-seeded.

---

### Issue 3 — P1: `liveSpot` and `_clientPayoffStub` miss quote data after initial load off-market

**Root cause:** Both `liveSpot` (line 1979) and `_clientPayoffStub` (line 2503) read
`_underlyingQuotes[selectedUnderlying]?.ltp` inside `untrack()`. The `untrack()` is intentional
(comment at 1973-1978: prevents OptionsPayoff SVG re-renders on every 5s wholesale `_underlyingQuotes`
replacement). The tracking dep is `_throttledTick` (SSE, 250ms gate) and `selectedUnderlying`.

Off-market: no SSE ticks → `_throttledTick` doesn't fire. After `loadUnderlyingQuotes()` populates
`_underlyingQuotes`, neither `liveSpot` nor the stub re-derives. Result: `liveSpot = 0` even though
the quote data is now available.

**Fix:** Add a `_quoteGeneration` counter ($state, incremented in `loadUnderlyingQuotes()` after
`_underlyingQuotes = next`). In `liveSpot` and `_clientPayoffStub`, check `isMarketOpen()` — if
off-market, track `_quoteGeneration` instead of relying solely on `_throttledTick`:
```js
// in liveSpot and _clientPayoffStub:
if (!isMarketOpen()) void _quoteGeneration;  // re-derive after quote load, off-market only
```
On-market, SSE ticks drive re-derivation (no change to existing path). Off-market, `_quoteGeneration`
bump triggers exactly one re-derive per quote load. No additional re-renders at the 5s poll cadence.

---

### Issue 4 — P2: Local `positions` stale — not reactive to book poller (line 4028-4030)

**Root cause:** The book poller (layout, 5s) updates `positionsStore.value`. Local `positions`
(line 3415) only updates when `loadPositions()` is called (onMount + fill events). The removed
periodic poll (line 4028-4030 comment) left no mechanism to propagate poller updates into `positions`.

**Consequence:** `_byUnderlyingTotals`, `_perRootReduce`, `_pnlByRootMap`, `_expPnlByRootMap`,
`candidatePositions`, `legs`, `_underlyingQuoteKeys` all read from stale `positions` between fills.
Per-row P&L and Exp P&L in the Snapshot grid can be hours out of date. The TOTAL row (`_snapshotTotalDay`)
reads `positionsStore.value` directly (live, 5s) — so TOTAL row ≠ sum(per-rows) during quiet periods.

**Fix:** Add a `$effect` that watches `positionsStore.value` and re-runs the synchronous F&O
transformation (no network call — reprocesses the data already in the store):
```js
$effect(() => {
  const rawPos = positionsStore.value;   // tracked dep → fires on every poller update
  if (!rawPos || !_positionsLoaded) return;
  untrack(() => {
    const merged = [];
    const excluded = {};
    for (const p of rawPos) {
      const sym = p?.tradingsymbol || p?.symbol;
      if (!sym) continue;
      if (!isFOSymbol(sym)) {
        bumpExcluded(excluded, p?.account, {
          pos_pnl: Number(p?.pnl || 0),
          pos_day: baseDayPnlForPosition(p),
        });
        continue;
      }
      const baseRow = buildPositionRowFromBroker(p, 'live');
      for (const row of splitClosedReopened(baseRow)) merged.push(row);
    }
    // Preserve sim positions from last explicit loadPositions() call
    const simRows = positions.filter(r => r.source === 'sim');
    positions = [...merged, ...simRows];
    _excludedByAccount = excluded;
  });
});
```
This fires every 5s (poller) and off-market. No extra network calls. Sim positions preserved.
`holdingsStore.value` analogous reactive effect added for `holdings` local state.

---

### Issue 5 — P1: Snapshot TOTAL row Day P&L not in sync with NavStrip (operator requirement)

**Operator requirement:** "Total row in snapshot should be in sync with NavStrip if there are no equities."
With no equity intraday positions, TOTAL row = pure F&O = NavStrip P1. The formula must be identical.

**Root cause:** `_snapshotTotalDay` (line 3426) uses `baseDayPnlForPosition(p)` which reads
`p.pnl` / `p.day_change_val` from the broker API response — last refreshed at most 5s ago.
`positionsDayPnlStore._store` (NavStrip source, `positionsDayPnlStore.svelte.js` lines 48-84) uses
`livePositionDayPnl(r, liveLtp, {marketOpen})` with `liveLtp = untrack(() => getSnapshot(sym)?.ltp)`
at 4Hz via `symbolTickCount` throttle. Between poller calls, the two diverge by live price movement × qty.

**Fix:** Mirror `positionsDayPnlStore._store` exactly in `_snapshotTotalDay`:
- Add `void _throttledTick` (already in scope on the page, drives 4Hz cadence)
- Import `livePositionDayPnl` from `$lib/data/nav.js` (not currently imported on derivatives page; `baseDayPnlForPosition` is)
- Use `untrack(() => getSnapshot(sym)?.ltp)` for per-symbol LTP (same as positionsDayPnlStore line 64)
- Iterate `positionsStore.value` (same source — not local `positions`)
- Apply `isMarketOpen()` flag (same as positionsDayPnlStore line 76)

```js
const _snapshotTotalDay = $derived.by(() => {
  void _throttledTick;                              // 4Hz SSE gate (same as positionsDayPnlStore)
  const matchAccount = buildAcctMatcher(selectedAccounts);
  let sum = 0;
  for (const p of (positionsStore.value ?? [])) {
    if (!matchAccount(String(p?.account || ''))) continue;
    const sym = String(p?.tradingsymbol || p?.symbol || '').toUpperCase();
    const liveLtp = sym ? untrack(() => getSnapshot(sym)?.ltp ?? null) : null;
    sum += livePositionDayPnl(
      {
        closePx: Number(p.previous_close) || Number(p.close_price ?? 0),
        pollLtp: Number(p.last_price ?? 0),
        qty:     Number(p.quantity ?? 0),
        avg:     Number(p.average_price ?? 0),
        dcvRow:  p,
      },
      liveLtp,
      { marketOpen: isMarketOpen() },
    );
  }
  return sum;
});
```

**Result:** When no account filter is active, `_snapshotTotalDay` = `positionsDayPnlStore.total`
exactly (same data source, same formula, same LTP). NavStrip P1 = Snapshot TOTAL. When equity
intraday positions exist, both NavStrip and TOTAL include them (both cover `positionsStore.value`)
so they still match. Account-filtered TOTAL correctly scopes to selected accounts (NavStrip
has no account granularity — this is a deliberate per-account view).

---

### Issue 6 — P2: `loadUnderlyingQuotes` paused off-market by `marketAwareInterval`

**Root cause:** Line 4033: `quotesTeardown = marketAwareInterval(loadUnderlyingQuotes, 5000, 10_000)`.
`marketAwareInterval` no-ops the main interval when `!isMarketOpen()`. Off-market, `_underlyingQuotes`
is only populated once (if Issue 2 is fixed). But without periodic refresh, it becomes stale.

Underlying quotes are needed off-market too — operator needs current spot for payoff chart positioning
and EV calculations. This is a REST call to `/api/quotes/batch`, not a tick subscription — the operator's
own stated rule ("no tick data refresh when market closed") doesn't apply here.

**Fix:** Change line 4033 from `marketAwareInterval` to `visibleInterval`:
```js
quotesTeardown = visibleInterval(loadUnderlyingQuotes, 5000, 'throttle:30000');
// 5s when visible, 30s when tab hidden — always runs, market-aware gate removed
```
On-market: same 5s cadence. Off-market: still runs at 5s (visible) or 30s (hidden).

---

### Issue 7 — P3: Snapshot TOTAL Day P&L includes equity intraday not shown in any row (design)

**Root cause (by design):** `_snapshotTotalDay` sums ALL `positionsStore.value` rows (equity intraday
+ F&O). Per-row values only cover F&O positions by underlying. This is intentional (TOTAL matches
NavStrip P1). But visually, sum(per-rows) ≠ TOTAL — looks like a calculation error.

**Fix (label only):** Add tooltip or sub-label on the TOTAL row: "All positions incl. equity intraday".
No formula change — this is correct by design.

---

## Agents

- backend: skip
- frontend: Apply all seven code fixes across two files:

  **`frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte` — Issue 8 (LTP reactive):**
  - Line 88: change `const ltp = $derived(lg && lg.ltp != null ? lg.ltp : c.ltp);`
    to `const ltp = $derived(getSnapshot(String(c.symbol || '').toUpperCase())?.ltp ?? (lg?.ltp ?? c.ltp));`
  - Import `getSnapshot` from `$lib/data/symbolStore.svelte.js` (not yet imported in this component)
  - This makes LTP SSE-reactive at 4Hz. The P&L formula at lines 105-115 reads `ltp` directly — it auto-becomes reactive once LTP is live. No other changes to this component.

  **`frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — Issues 1–7:**
  1. **Issue 1** (~line 3753): add `&& _positionsLoaded && instrumentsReady` to the strategy-wipe guard

  2. **Issue 2** (~line 3666, end of `loadPositions()`): add `loadUnderlyingQuotes();` fire-and-forget after `positions = merged`
  3. **Issue 3**: add `let _quoteGeneration = $state(0)` near `_underlyingQuotes` declaration; increment it in `loadUnderlyingQuotes()` after `_underlyingQuotes = next`; in `liveSpot` and `_clientPayoffStub`, add `if (!isMarketOpen()) void _quoteGeneration;` before the `untrack()` bq read
  4. **Issue 4**: add the `$effect` that watches `positionsStore.value` and re-runs the F&O transformation synchronously; add analogous `$effect` for `holdingsStore.value`
  5. **Issue 5** (~line 3426): rewrite `_snapshotTotalDay` with `void _throttledTick` + live LTP override for overnight positions
  6. **Issue 6** (line 4033): change `marketAwareInterval(loadUnderlyingQuotes, 5000, 10_000)` → `visibleInterval(loadUnderlyingQuotes, 5000, 'throttle:30000')`
  7. **Issue 7** (Snapshot TOTAL row template): add `title` attribute or small muted label "(all positions)"

  For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional.
  No change ships without a corresponding test update.

- broker: skip
- doc: skip
- backend-test: skip
- playwright: Add/update Playwright specs covering:
  1. Off-market cold-start: load derivatives page with prior sessionStorage strategy → payoff chart renders (strategy NOT wiped)
  2. Underlying switch: after switching selectedUnderlying to CRUDEOIL, `liveSpot` is non-zero within 3s (quote picked up even off-market)
  3. Snapshot TOTAL vs per-row sum consistency: after page load, Snapshot TOTAL Day P&L matches `positionsDayPnlStore.total` within 1% tolerance
  4. Positions sync: simulate book poller update (stub `positionsStore.value` change) → verify per-row P&L updates within one render cycle

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message
fix(derivatives): unify reactive chain — positions$effect, quoteGeneration, loadStrategy guard, quotes always-on

## Done when
- Snapshot per-row P&L / Exp P&L update within 5s of book poller refresh (no fill required)
- Payoff chart renders on cold page load when prior session cached strategy (no blank flash)
- CRUDEOIL/MCX liveSpot is non-zero after loadPositions() completes, off-market
- `_snapshotTotalDay` matches `positionsDayPnlStore.total` (NavStrip P1) within ticker resolution during market hours; when no equity positions, TOTAL row = NavStrip exactly
- `loadUnderlyingQuotes` runs off-market (5s visible / 30s hidden)
- Legs LTP column shows live SSE price (same tick-by-tick as market data, not last poll)
- Legs P&L column updates reactively when LTP changes (derived from live LTP)
- Legs Qty / Avg cost in sync with `positionsStore.value` within 5s (SSOT with Pulse)
- Legs Day P&L and Exp P&L already reactive — verify still working after change
- svelte-check: 0 errors
- Playwright specs green
