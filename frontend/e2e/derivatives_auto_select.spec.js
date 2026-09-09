/**
 * derivatives_auto_select.spec.js
 *
 * Static source-inspection spec — verifies the derivatives overlay
 * auto-select uses |qty| as the primary sort key (CRUDEOIL with 2 lots
 * beats COPPER with 1 lot), and that the reverted localStorage /
 * _provisionalSeed complexity is NOT present.
 */
import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const PAGE_PATH = '/Users/ramanambore/projects/ramboq/frontend/src/routes/(algo)/admin/derivatives/+page.svelte';

test.describe('Derivatives overlay auto-select: qty-sort', () => {
  test('1-SSOT: _rootQtySum map is declared in underlyingOptionsForPicker', () => {
    const src = readFileSync(PAGE_PATH, 'utf-8');
    expect(src).toContain('const _rootQtySum = new Map()');
  });

  test('2-SSOT: _rootQtySum accumulates |qty| per root', () => {
    const src = readFileSync(PAGE_PATH, 'utf-8');
    expect(src).toMatch(/_rootQtySum\.set\(r,\s*\(_rootQtySum\.get\(r\)\s*\|\|\s*0\)\s*\+\s*Math\.abs/);
  });

  test('3-SSOT: Tier 1 sort uses _rootQtySum as primary key', () => {
    const src = readFileSync(PAGE_PATH, 'utf-8');
    // _rootQtySum.get(b) - _rootQtySum.get(a) must appear before _rootPosCount in Tier 1 sort
    const qtyIdx = src.indexOf('_rootQtySum.get(b)');
    const cntIdx = src.indexOf('_rootPosCount.get(b)');
    expect(qtyIdx).toBeGreaterThan(-1);
    expect(qtyIdx).toBeLessThan(cntIdx);
  });

  test('4-SSOT: Tier 2 sort also uses _rootQtySum as primary key', () => {
    const src = readFileSync(PAGE_PATH, 'utf-8');
    // Should have at least 2 occurrences of the qty-sort pattern
    const matches = src.match(/_rootQtySum\.get\(b\)/g) || [];
    expect(matches.length).toBeGreaterThanOrEqual(2);
  });

  test('5-regression: _provisionalSeed must NOT be present', () => {
    const src = readFileSync(PAGE_PATH, 'utf-8');
    expect(src).not.toContain('_provisionalSeed');
  });

  test('6-regression: localStorage auto-restore must NOT be present', () => {
    const src = readFileSync(PAGE_PATH, 'utf-8');
    expect(src).not.toContain("localStorage.getItem('ramboq.derivatives.underlying')");
    expect(src).not.toContain("localStorage.setItem('ramboq.derivatives.underlying'");
  });
});
