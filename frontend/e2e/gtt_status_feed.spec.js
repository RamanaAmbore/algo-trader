/**
 * gtt_status_feed.spec.js
 *
 * The order card used to show each attached GTT's live broker status via
 * a `gttLegs` prop (sourced from `attached_gtts_json`, populated
 * exclusively by the now-deleted Bracket/template-attach engine) and the
 * order log used to emit `'gtt'`/`'gtt_missing'` events for those same
 * order-attached legs. Both removed 2026-11 (Track 3, T3.4,
 * Bracket/Template/GTT removal) — see OrderCard.svelte and
 * LogPanel.svelte's own comments at the removal sites.
 *
 * The genuinely independent standalone-broker-GTT viewer (`fetchGtts()`,
 * the `'gtt_broker'` event kind in LogPanel's own order log) is UNRELATED
 * to Bracket and was NOT touched by T3.4 — still covered below.
 *
 * OrderBook.svelte's OWN "GTT" chip/card presentation of that SAME
 * standalone viewer (a distinct UI surface from LogPanel's order-log
 * line, not a Bracket leftover) was separately removed 2026-11, per
 * operator: "we don't need gtt count status pills" — covered below too.
 * `orders_gtt.py`'s backend route is untouched either way; LogPanel is
 * still a real, live caller of it.
 *
 * Source-level check, no browser session needed.
 */
import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dir = dirname(fileURLToPath(import.meta.url));
const CARD = readFileSync(join(__dir, '../src/lib/order/OrderCard.svelte'), 'utf8');
const BOOK = readFileSync(join(__dir, '../src/lib/OrderBook.svelte'), 'utf8');
const LOG = readFileSync(join(__dir, '../src/lib/LogPanel.svelte'), 'utf8');

test('order card no longer accepts a gttLegs prop or renders per-leg GTT chips (removed 2026-11 T3.4)', () => {
  expect(CARD).not.toMatch(/gttLegs/);
  expect(CARD).not.toMatch(/gtt-leg-missing/);
});

test('order book no longer matches order rows against broker GTTs for a per-order leg display (removed 2026-11 T3.4)', () => {
  expect(BOOK).not.toMatch(/import \{ matchGtts \} from '\$lib\/data\/gttMatch\.js'/);
  expect(BOOK).not.toMatch(/gttLegs=/);
});

test('order log no longer emits per-order-leg gtt/gtt_missing events, but still emits the independent gtt_broker event', () => {
  expect(LOG).toMatch(/fetchGtts\(\)\.then/);
  expect(LOG).not.toMatch(/kind:\s*l\.missing \? 'gtt_missing' : 'gtt'/);
  expect(LOG).toMatch(/kind:\s*'gtt_broker'/);
});

// ── OrderBook's own standalone-GTT chip/card presentation (removed 2026-11,
// per operator: "we don't need gtt count status pills" — a different UI
// surface from LogPanel's 'gtt_broker' order-log line above, not Bracket).
test('order book no longer imports fetchGtts/cancelGtt or renders a GTT chip/card', () => {
  const importLine = BOOK.split('\n').find((l) => l.includes("from '$lib/api'"));
  expect(importLine).not.toMatch(/fetchGtts/);
  expect(importLine).not.toMatch(/cancelGtt/);
  expect(BOOK).not.toMatch(/fetchGtts\(\)/);
  expect(BOOK).not.toMatch(/cancelGtt\(/);
  expect(BOOK).not.toMatch(/gtt-card/);
  expect(BOOK).not.toMatch(/data-status="gtt"/);
  expect(BOOK).not.toMatch(/_filteredGttRows/);
});

// ── Replaces the deleted order_book_poll_inflight_guard.spec.js, whose
// isolation mechanism (intercepting /orders/gtts/ as the one endpoint
// exclusive to OrderBook's own guarded poll) no longer has an exclusive
// endpoint to intercept now that OrderBook never calls fetchGtts() at
// all — it would otherwise observe zero requests and vacuously pass.
// withGuard's own unit mechanics stay covered by withGuard.test.js; this
// just pins that OrderBook's onMount still wires BOTH the immediate call
// and the visibleInterval tick through the SAME guarded instance.
test('order book wraps its poll loop in exactly one withGuard-wrapped _loadOrders, shared by the immediate call and every interval tick', () => {
  expect(BOOK).toMatch(/const _guardedLoadOrders = withGuard\(_loadOrders\);/);
  const mountBlock = BOOK.slice(BOOK.indexOf('const _guardedLoadOrders = withGuard(_loadOrders);'));
  const nextFn = mountBlock.indexOf('\n  }', 40);
  const scoped = mountBlock.slice(0, nextFn > 0 ? nextFn : undefined);
  expect(scoped).toMatch(/_guardedLoadOrders\(\);/);
  expect(scoped).toMatch(/visibleInterval\(\(\) => \{ _guardedLoadOrders\(\); \}, pollMs\)/);
});
