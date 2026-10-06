/**
 * template_toggle_off.spec.js
 *
 * Turning the template toggle off sends no template. It does not send the
 * "none" template row, which would record an attach attempt on the order.
 * Source-level check, no browser session needed.
 */
import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dir = dirname(fileURLToPath(import.meta.url));
const SP = readFileSync(join(__dir, '../src/lib/SymbolPanel.svelte'), 'utf8');
const OT = readFileSync(join(__dir, '../src/lib/order/OrderTicket.svelte'), 'utf8');

test('toggle off clears the shared template id to null', () => {
  const block = SP.match(/onSelectNone=\{\(\) => \{([\s\S]*?)\}\}/);
  expect(block, 'onSelectNone present').not.toBeNull();
  expect(block[1]).toMatch(/_sharedTemplateId = null;/);
  expect(block[1]).not.toMatch(/_noneTpl\.id/);
});

test('remembered off preference resolves to no template', () => {
  expect(SP).toMatch(/if \(pref === 'none'\) \{\s*_sharedTemplateId = null;/);
});

test('order ticket never falls back to the none row', () => {
  expect(OT).not.toMatch(/templateId = none\.id/);
});
