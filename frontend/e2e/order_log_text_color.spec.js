/**
 * order_log_text_color.spec.js
 *
 * The order log message text takes its colour from the row level, as the
 * other log tabs do. A fixed off-white on .log-row-msg made it differ.
 *
 * Source-level check: reads the component file directly, so it needs no
 * backend or browser session.
 */
import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dir = dirname(fileURLToPath(import.meta.url));
const SRC = readFileSync(join(__dir, '../src/lib/LogPanel.svelte'), 'utf8');

test('order log message text does not override the row level colour', () => {
  const rule = SRC.match(/\.lp-order-scroll \.log-row-msg \{([\s\S]*?)\}/);
  expect(rule, '.log-row-msg rule present').not.toBeNull();
  expect(rule[1]).not.toMatch(/\bcolor\s*:/);
});
