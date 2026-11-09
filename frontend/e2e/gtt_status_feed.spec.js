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
 * the `'gtt_broker'` event kind, OrderBook.svelte's own "GTT" chip
 * section) is UNRELATED to Bracket and was NOT touched — still covered
 * below.
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
