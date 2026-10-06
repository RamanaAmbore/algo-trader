/**
 * gtt_status_feed.spec.js
 *
 * The order card shows each attached GTT's live broker status, and the order
 * log shows GTT legs, missing legs, and broker GTTs with no order.
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

test('order card renders a chip per matched GTT leg', () => {
  expect(CARD).toMatch(/gttLegs\s*=\s*\[\]/);
  expect(CARD).toMatch(/\{#each gttLegs as leg/);
  expect(CARD).toMatch(/gtt-leg-missing/);
});

test('order book passes each order its matched legs', () => {
  expect(BOOK).toMatch(/import \{ matchGtts \} from '\$lib\/data\/gttMatch\.js'/);
  expect(BOOK).toMatch(/gttLegs=\{_gttMatch\.legsByOrder\.get\(_oKey\)/);
});

test('order log fetches broker GTTs and emits leg, missing, and broker events', () => {
  expect(LOG).toMatch(/fetchGtts\(\)\.then/);
  expect(LOG).toMatch(/kind:\s*l\.missing \? 'gtt_missing' : 'gtt'/);
  expect(LOG).toMatch(/kind:\s*'gtt_broker'/);
});
