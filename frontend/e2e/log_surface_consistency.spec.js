/**
 * log_surface_consistency.spec.js
 *
 * Log surfaces share the reference style from the news and system/conn logs:
 * cyan time at the small size, muted tag at the small size, and a message at
 * the extra-small size. Source-level check, no browser session needed.
 */
import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dir = dirname(fileURLToPath(import.meta.url));
const LOG = readFileSync(join(__dir, '../src/lib/LogPanel.svelte'), 'utf8');
const UL = readFileSync(join(__dir, '../src/lib/UnifiedLog.svelte'), 'utf8');

test('order log tag uses the shared tag style, not a local override', () => {
  expect(LOG).not.toMatch(/\.lp-order-scroll \.log-row-tag \{/);
});

test('unified log time uses the reference cyan at the small size', () => {
  const rule = UL.match(/\.ul-time \{([\s\S]*?)\}/);
  expect(rule, '.ul-time rule present').not.toBeNull();
  expect(rule[1]).toMatch(/color:\s*#7dd3fc/);
  expect(rule[1]).toMatch(/font-size:\s*var\(--fs-sm\)/);
});
