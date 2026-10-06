/**
 * order_log_events.spec.js
 *
 * The order log shows events for every order, not only open ones, and maps
 * algo order statuses (FILLED, UNFILLED, CANCEL_FAILED) to their events.
 * Source-level check, no browser session needed.
 */
import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dir = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(join(__dir, '../src/lib/LogPanel.svelte'), 'utf8');

test('order events are fetched for all orders, not only open ones', () => {
  expect(SRC).toMatch(/fetchOrderEvents\(200, 'all'\)/);
});

test('algo FILLED status produces a fill event', () => {
  expect(SRC).toMatch(/st === 'FILLED' \|\| st === 'COMPLETE'/);
});

test('unfilled and cancel-failed statuses produce their own events', () => {
  expect(SRC).toMatch(/st === 'UNFILLED'/);
  expect(SRC).toMatch(/st === 'CANCEL_FAILED'/);
});

test('template attach events have colours', () => {
  expect(SRC).toMatch(/case 'template_attach_ok'/);
  expect(SRC).toMatch(/case 'template_attach_failed'/);
});
