// mcx_dst_aware_close_time.spec.js — MCX's evening close is DST-aware
// (23:30 while US DST is active, 23:55 during US winter/standard time),
// not a flat 23:30. This closes a residual left behind by the backend
// MCX-DST fix (commits 3c787204, b30c96f4, 101ea83d, 170d3394, 6816a626,
// f7b01b7c, 0c30fed3): `frontend/src/lib/marketHours.js` carried its own
// static `MCX_CLOSE_MIN = 23 * 60 + 30` with zero DST awareness, used by
// OrderTicket.svelte's "market closed" gate (via `isMcxOpen()`) for the
// time-of-day fallback path (before the first `/api/market/status` poll
// lands, or when it fails). See CLAUDE.md "MCX DST-aware non-agri close
// time" and `frontend/src/lib/data/mcxCloseTime.js`.
//
// Source-grep guard — no server/auth needed, so it runs everywhere
// including CI. The real behavioural logic is covered exhaustively by
// the Vitest unit suite (`src/lib/__tests__/data/mcxCloseTime.test.js`),
// which exercises the actual resolver against the real US DST calendar
// (transition boundaries computed programmatically, never hardcoded) —
// this spec only pins that the gate's SOURCE actually wires through it,
// per the project's "no exceptions" test-coverage rule for a
// `frontend/src/lib/*.js` change.
//
// Five quality dimensions (feedback_test_dimensions.md):
//   1. SSOT    — asserts marketHours.js's three MCX-close consumers
//                (isMcxOpen, isMarketOpen, isBookSnapshotGraceWindow)
//                all resolve through the one shared resolver, never a
//                second independent copy.
//   2. Perf    — pure fs read, no browser/server.
//   3. Stale   — this IS the stale-code guard (the exact literal the
//                live incident traced back to).
//   4. Reuse   — confirms OrderTicket.svelte's order-ticket gate
//                delegates to isMcxOpen()/isNseOpen() rather than
//                carrying its own close-time literal.
//   5. UX      — a stale "Market closed" banner on a genuinely
//                placeable MCX order is a direct, misleading UX defect;
//                this guard exists specifically to stop that literal
//                from reappearing.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

function readSrc(relPath) {
  return readFileSync(new URL(relPath, import.meta.url).pathname, 'utf8');
}

test('marketHours.js resolves MCX close time via the DST-aware resolver, not a flat literal', () => {
  const src = readSrc('../src/lib/marketHours.js');

  // The fix: imports and uses the shared resolver.
  expect(src).toContain("from './data/mcxCloseTime.js'");
  expect(src).toContain('mcxNonAgriCloseMinuteOfDay');

  // The regression: a flat, DST-unaware MCX close constant must never
  // reappear in this file.
  expect(src).not.toMatch(/const\s+MCX_CLOSE_MIN\s*=\s*23\s*\*\s*60\s*\+\s*30/);
  expect(src).not.toMatch(/const\s+ANY_CLOSE_MIN\s*=\s*MCX_CLOSE_MIN/);
});

test('every MCX-close consumer in marketHours.js routes through the one shared resolver', () => {
  const src = readSrc('../src/lib/marketHours.js');

  // isMcxOpen, isMarketOpen, and isBookSnapshotGraceWindow all derive
  // their MCX close boundary from _mcxCloseMinFor()/mcxNonAgriCloseMinuteOfDay
  // — never a second, independently-hardcoded close-time literal.
  const occurrences = src.match(/_mcxCloseMinFor\(/g) || [];
  expect(occurrences.length).toBeGreaterThanOrEqual(2); // isMcxOpen + isMarketOpen
  expect(src).toMatch(/_postCloseGraceSameDay\(minute,\s*_mcxCloseMinFor\(parts\)\)/);
  expect(src).toMatch(/_postCloseGraceWrapFromYesterday\(minute,\s*_mcxCloseMinFor\(yParts\)\)/);
});

test('mcxCloseTime.js resolves DST via the real America/New_York zone, never a hardcoded date range', () => {
  const src = readSrc('../src/lib/data/mcxCloseTime.js');

  expect(src).toContain('America/New_York');
  expect(src).toMatch(/hour:\s*23,\s*minute:\s*30/);
  expect(src).toMatch(/hour:\s*23,\s*minute:\s*55/);

  // Must never hand-roll a fixed year-specific transition date — the
  // DST verdict is derived from Intl.DateTimeFormat's own zone offset
  // for the target instant, not a literal calendar cutoff.
  expect(src).not.toMatch(/getDate\(\)\s*[<>=]=?\s*\d{1,2}/);
  expect(src).not.toMatch(/\b202\d-\d{2}-\d{2}\b/); // no hardcoded transition date
});

test("OrderTicket.svelte's market-open gate delegates to isMcxOpen()/isNseOpen(), not its own close-time literal", () => {
  const src = readSrc('../src/lib/order/OrderTicket.svelte');

  expect(src).toContain('function _mktOpenForExchange(');
  expect(src).toMatch(/_mktOpenForExchange[\s\S]{0,200}isMcxOpen\(\)/);
  // No independent MCX close-time literal of its own — it must always
  // read through marketHours.js's isMcxOpen(), which this fix makes
  // DST-aware for every caller at once.
  expect(src).not.toMatch(/23\s*\*\s*60\s*\+\s*30/);
});
