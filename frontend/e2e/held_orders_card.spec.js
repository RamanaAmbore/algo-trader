/**
 * held_orders_card.spec.js
 *
 * The held-orders card lists held automated orders with a release action and is
 * mounted on the orders page. Source-level check, no browser session needed.
 */
import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dir = dirname(fileURLToPath(import.meta.url));
const CARD = readFileSync(join(__dir, '../src/lib/HeldOrdersCard.svelte'), 'utf8');
const PAGE = readFileSync(join(__dir, '../src/routes/(algo)/orders/+page.svelte'), 'utf8');

test('held card has a release action per order', () => {
  expect(CARD).toMatch(/aria-label="Held orders"/);
  expect(CARD).toMatch(/onclick=\{\(\) => release\(row\)\}/);
  expect(CARD).toMatch(/>Release</);
});

test('orders page mounts the held card above the order book', () => {
  expect(PAGE).toMatch(/import HeldOrdersCard from '\$lib\/HeldOrdersCard\.svelte'/);
  expect(PAGE).toMatch(/<HeldOrdersCard \/>\s*\n\s*<OrderBook/);
});
