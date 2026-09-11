# Plan: Frontend debug-logging framework + payoff/NavStrip instrumentation

## Context

Diagnosing frontend issues (payoff chart blank, spot resolution failures, NavStrip exp-profit mismatch) requires repeatedly deploying debug builds and reading raw console noise. A lightweight, always-on-in-dev, zero-overhead-in-prod logging framework eliminates this: enable a namespace in the browser console, reproduce the issue, dump the decision trace.

The payoff chart and NavStrip exp-profit discrepancies both trace through the same 6-7 key decision points (spot resolution tiers, batchQuote results, SSE tick routing, strategy loading, stub computation). Instrumenting those points makes every future diagnosis a 30-second console dump instead of a multi-day code audit.

## Task

### Part 1 — `debugLog.js` module

Create `frontend/src/lib/debug/debugLog.js`:

```js
// Usage: window.__RAMBOQ_DEBUG = 'payoff' | 'sse' | true | false
// Dump:  copy(window.__RAMBOQ_DUMP('payoff'))  → JSON to clipboard

const _ring = [];

export function debugLog(ns, event, data) {
  if (!globalThis.__RAMBOQ_DEBUG) return;
  const filter = globalThis.__RAMBOQ_DEBUG;
  if (typeof filter === 'string' && !ns.startsWith(filter)) return;
  const entry = { ts: Date.now(), ns, event, data };
  _ring.push(entry);
  if (_ring.length > 500) _ring.shift();
  console.debug(`[RQ:${ns}] ${event}`, data ?? '');
}

if (typeof globalThis !== 'undefined') {
  globalThis.__RAMBOQ_DUMP = (ns) => {
    const rows = ns ? _ring.filter(e => e.ns === ns || e.ns.startsWith(ns + ':')) : _ring;
    return JSON.stringify(rows, null, 2);
  };
  globalThis.__RAMBOQ_DOWNLOAD = (ns) => {
    const json = globalThis.__RAMBOQ_DUMP(ns);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([json], { type: 'application/json' }));
    a.download = `ramboq-debug-${ns || 'all'}-${Date.now()}.json`;
    a.click();
  };
}
```

Zero overhead when `window.__RAMBOQ_DEBUG` is falsy — the guard exits before any string formatting or allocation.

### Part 2 — Instrumentation points

Instrument these 8 points (import `debugLog` in each file, add one-liner calls):

| Namespace | File | Event | Key data |
|---|---|---|---|
| `sse` | `quoteStream.js` | `snapshot` | symbol count, first 3 syms, ts |
| `sse` | `quoteStream.js` | `tick` | sym, ltp |
| `sse` | `quoteStream.js` | `connect/error/reconnect` | backoffMs |
| `payoff:bq` | `derivatives/+page.svelte` → `loadUnderlyingQuotes` | `request` | keys array |
| `payoff:bq` | `derivatives/+page.svelte` → `loadUnderlyingQuotes` | `result` | {sym→ltp} map |
| `payoff:anchor` | `derivatives/+page.svelte` → tickBus anchor bridge | `tick` | root, stratUnd, ltp |
| `payoff:spot` | `derivatives/+page.svelte` → `liveSpot` | `resolved` | tier (1a/1b/2/3/4/stub), value |
| `payoff:stub` | `derivatives/+page.svelte` → `_clientPayoffStub` | `spot` | tier, value, legs count |
| `payoff:strategy` | `derivatives/+page.svelte` → strategy load | `loaded` | underlying, anchor, legs count, spot |
| `payoff:merge` | `derivatives/+page.svelte` → `_mergedPayoff` | `computed` | length, spot range min/max |
| `navstrip:spot` | `PositionStrip.svelte` → `_loadUnderlyingSpots` | `request/result` | keys, {sym→ltp} |
| `navstrip:expiry` | `PositionStrip.svelte` → `_expiryProfit` | `computed` | total, leg count, skipped count |

For `liveSpot`, the log must emit which tier fired and the resolved value — this is the critical trace for the payoff chart blank issue.

For the SSE namespace: log on connect, each snapshot (count only), and each error — NOT on every tick (too noisy). Ticks logged only for `window.__RAMBOQ_DEBUG === 'sse:tick'`.

### Part 3 — Fix: NavStrip exp profit vs Snapshot mismatch

Root cause: `_hExpNetTotal = _snapshotTotalExp + _hPnlTotal` mixes F&O expiry P&L (intrinsic at spot) with equity holdings LIFETIME broker P&L. These are different metrics. The column should either:
- Show F&O expiry + equity **expiry-equivalent** P&L (= `_snapshotTotalExp + sum(_hExpByRoot)`)
- Or label it clearly so users know it includes equity lifetime P&L

NavStrip P-slot-3 (54K) = F&O-only intrinsic expiry. Snapshot "Exp P&L Net" (4.15L) = F&O expiry + equity lifetime P&L. The fix: change Snapshot "Exp P&L Net" to use `_hExpTotal` (sum of `_hExpByRoot`) instead of `_hPnlTotal` so both use the same "intrinsic expiry at current spot" formula for equity and F&O. After the fix, NavStrip + Snapshot should agree (both = F&O expiry + equity expiry-equivalent P&L at current spot).

Files to change for Part 3:
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — find `_hExpNetTotal` computation and change `_hPnlTotal` → sum of `_hExpByRoot` values

## Agents

- backend: skip
- frontend: Implement all three parts as described:
  1. Create `frontend/src/lib/debug/debugLog.js` with the exact implementation above.
  2. Instrument the 8 namespaces listed in Part 2 — import `debugLog` in each file, add calls. For `liveSpot` in derivatives/+page.svelte, the log must track which tier (1a=anchor, 1b=stratUnd, 2=resolvedTs, 3=pos scan, 4=batchQuote, stub) succeeded and the value. Add `debugLog('payoff:spot', 'resolved', { tier, value })` at each return path in the `liveSpot` derived block.
  3. Fix `_hExpNetTotal` in derivatives/+page.svelte: change from `_snapshotTotalExp + _hPnlTotal` to `_snapshotTotalExp + Object.values(_hExpByRoot).reduce((s, v) => s + (v ?? 0), 0)` (sum of equity expiry-equivalent P&L). Update the `_hExpNetTotal` variable accordingly.
  4. Write or update Vitest tests in `frontend/src/lib/__tests__/` covering `debugLog` (ring buffer, namespace filter, dump/download globals) and the `_hExpNetTotal` formula change.

  For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional.

- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

feat(debug): frontend debugLog framework + payoff/NavStrip instrumentation + fix snapshot Exp P&L Net formula

## Done when

- `window.__RAMBOQ_DEBUG = 'payoff'` in browser console enables structured trace logs for the payoff chart
- `window.__RAMBOQ_DOWNLOAD('payoff')` downloads the ring buffer as JSON
- `svelte-check` passes with 0 errors
- Snapshot "Exp P&L Net" = F&O expiry + equity expiry-equivalent P&L (matches NavStrip P-slot-3 + equity expiry)
- Vitest passes (ring buffer, namespace filter, dump function all tested)
