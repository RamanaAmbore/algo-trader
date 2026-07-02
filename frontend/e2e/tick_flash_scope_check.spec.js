import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Static grep assertions that enforce the tick-flash scope contract:
//   - MarketPulse + PerformancePage MUST NOT import from tickFlash.svelte.js
//   - derivatives/+page.svelte MUST import from tickFlash.svelte.js
//
// These run without a server and complete in milliseconds.

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const readSrc = (relPath) =>
  readFileSync(path.resolve(__dirname, '..', relPath), 'utf-8');

test.describe('tick-flash scope invariants', () => {
  test('MarketPulse does not import createTickFlash', () => {
    const src = readSrc('src/lib/MarketPulse.svelte');
    expect(src).not.toContain('createTickFlash');
  });

  test('PerformancePage does not import createTickFlash', () => {
    const src = readSrc('src/lib/PerformancePage.svelte');
    expect(src).not.toContain('createTickFlash');
    expect(src).not.toContain("from '$lib/data/tickFlash.svelte.js'");
  });

  test('derivatives +page.svelte imports createTickFlash (invariant preserved)', () => {
    const src = readSrc('src/routes/(algo)/admin/derivatives/+page.svelte');
    expect(src).toContain("from '$lib/data/tickFlash.svelte.js'");
    expect(src).toContain('createTickFlash');
  });

  test('MarketPulse has no dangling _mpFlash references', () => {
    const src = readSrc('src/lib/MarketPulse.svelte');
    expect(src).not.toContain('_mpFlash');
    expect(src).not.toContain('createTickFlash');
  });

  test('PerformancePage has no dangling _perfFlash references', () => {
    const src = readSrc('src/lib/PerformancePage.svelte');
    expect(src).not.toContain('_perfFlash');
    expect(src).not.toContain('pnlClsFlash');
    expect(src).not.toContain('avgVsLtpClsFlash');
  });

  test('PositionStrip preserves its flash import (pre-rollout baseline untouched)', () => {
    const src = readSrc('src/lib/PositionStrip.svelte');
    expect(src).toContain('createTickFlash');
  });

  test('NavCard preserves its flash import (pre-rollout baseline untouched)', () => {
    const src = readSrc('src/lib/NavCard.svelte');
    expect(src).toContain('createTickFlash');
  });
});
