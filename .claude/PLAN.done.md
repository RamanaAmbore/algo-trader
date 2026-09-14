# Plan: P&L Sync Fixes — Legs Day P&L double-count + NavStrip Exp P&L + Market page null guard

## Context

Three bugs identified during live debugging session on 2026-09-14.
Fixes already applied to `dev` branch (commits `2c18ac25`, `b73df94f`) but `workshop` is behind.
This plan ports them to `workshop` so the pipeline flows correctly: workshop → ddev → dprod.

---

## Fix 1 — Market page crash on Gemini timeout

**File**: `frontend/src/routes/(public)/market/+page.svelte` (lines 92–95)

`_request()` in `api.js` returns `null` (not throws) when the 15s AbortController fires.
`fetchMarket()` can return `null`. The page then did `data.content` → TypeError on null receiver before `??` fires.

```js
// Before
const data  = await fetchMarket();
content     = data.content ?? '';
lastRefresh = data.refreshed_at ?? '';
dataCache.market = data;

// After
const data  = await fetchMarket();
content     = data?.content ?? '';
lastRefresh = data?.refreshed_at ?? '';
if (data) dataCache.market = data;
```

---

## Fix 2 — Legs Day P&L double-counting (9.90L shown, 4.50L correct)

**File**: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` (line ~1186)

**Root cause**: `_candDayPnl` looked up `positionsDayPnlStore.byKey[sym]` which accumulates Day P&L across ALL accounts for a symbol (`byKey[sym] = (byKey[sym] ?? 0) + val`). But `candidatePositions` has per-account rows from `buildCandidatePositions` (`pageLoad.js` line 318: `real.push({ ...p, kind })`). Two accounts holding the same CRUDEOIL contract → each row returns the combined total → 2× double-count.

```js
// Before
const _candDayPnl = (c) => {
  const sym = String(c?.tradingsymbol || c?.symbol || '').toUpperCase();
  return positionsDayPnlStore.byKey[sym] ?? baseDayPnlForPosition(c);
};

// After — per-row, SSE-rescued, no cross-account aggregation
const _candDayPnl = (c) => {
  const sym  = String(c?.symbol || c?.tradingsymbol || '').toUpperCase();
  const snap = untrack(() => getSnapshot(sym));
  return livePositionDayPnl(
    {
      closePx: c.prev_close ?? 0,
      pollLtp: c.ltp        ?? 0,
      qty:     c.qty        ?? 0,
      avg:     c.avg_cost   ?? 0,
      dcvRow:  c,
    },
    snap?.ltp ?? null,
    { marketOpen: isMarketOpen() },
  );
};
```

All imports already present (`livePositionDayPnl`, `getSnapshot`, `untrack`, `isMarketOpen`).
Candidate `c` fields (`prev_close`, `ltp`, `qty`, `avg_cost`, `day_change_val`, `overnight_quantity`, `pnl`, `prev_settlement_pnl`) come from `buildPositionRowFromBroker` and map correctly.

---

## Fix 3 — NavStrip Exp P&L spot divergence (37k shown, ~2.79L correct)

**File**: `frontend/src/lib/PositionStrip.svelte` — `_resolveOptionSpot` function

**Root cause**: `_resolveOptionSpot` prioritised the SSE symbolStore over `p.underlying_ltp` (backend-stamped per position row from positions.py Pass 3). For MCX CRUDEOIL options, the SSE store could resolve to a stale/wrong front-month futures LTP. The derivatives snapshot (`_expPnlByRootMap`) correctly reads `p.underlying_ltp` first. NavStrip must use the same priority order: `underlying_ltp` first, symbolStore fallback.

---

## Agents

- frontend: Port both dev commits (`2c18ac25` and `b73df94f`) to workshop. Cherry-pick or re-implement the three changes above. Verify exact diffs against dev before committing to workshop.
- backend: skip
- broker: skip
- backend-test: skip
- doc: skip
- playwright: skip

## Tests

- pytest: no
- svelte-check: yes
- vitest: yes

## Commit message

fix(derivatives,market): legs Day P&L double-count + NavStrip Exp P&L spot + market null guard

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>

## Done when

- `candidatesDayPnl` on derivatives page matches `_fnoDayPnlByRoot.total` (both ~4.50L, not 9.90L)
- NavStrip Exp P&L matches derivatives snapshot Exp P&L (~2.79L, not 37k)
- Market page handles Gemini timeout gracefully (blank content, no crash)
- svelte-check 0 errors, vitest passes
