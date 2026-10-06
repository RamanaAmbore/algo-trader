/**
 * ticket_no_default_template.spec.js
 *
 * The order ticket does not pick a default exit template on its own. The
 * operator chooses one. Source-level check, no browser session needed.
 */
import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dir = dirname(fileURLToPath(import.meta.url));
const SP = readFileSync(join(__dir, '../src/lib/SymbolPanel.svelte'), 'utf8');
const OT = readFileSync(join(__dir, '../src/lib/order/OrderTicket.svelte'), 'utf8');

test('symbol panel leaves the template empty when the side changes', () => {
  expect(SP).not.toMatch(/_sharedTemplateId = _sideAwareDefault\?\.id/);
});

test('order ticket first paint does not pick a default template', () => {
  expect(OT).not.toMatch(/sideMatch\.id/);
  expect(OT).not.toMatch(/bothMatch\.id/);
});
