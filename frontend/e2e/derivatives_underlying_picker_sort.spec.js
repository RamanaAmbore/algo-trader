/**
 * derivatives_underlying_picker_sort.spec.js
 *
 * Guards the sort-key bug fix in underlyingOptionsForPicker:
 * the picker must rank roots by total abs(qty) accumulated across all legs,
 * NOT by leg count (+1 per position row).
 *
 * Before the fix: _rootPosCount incremented by 1 per leg, so COPPER with
 * 2 option legs (CE + PE each qty=1) outranked CRUDEOIL with 1 futures leg
 * at qty=2, causing COPPER to auto-select instead of CRUDEOIL.
 *
 * After the fix: _rootQtySum accumulates Math.abs(Number(p.qty || 0)), so
 * the root with the highest total absolute quantity wins.
 *
 * Five quality dimensions:
 *  1. SSOT   — _rootQtySum is the single accumulator; _rootPosCount is gone.
 *  2. Perf   — source-grep only; zero network round-trips, zero server deps.
 *  3. Stale  — asserts old variable name is absent (regression guard).
 *  4. Reuse  — no new state vars introduced; fix is local to the $derived.by block.
 *  5. UX     — sort comparators use qty-weighted key in both Tier 1 and Tier 2.
 *
 * Run (no server required):
 *   npx playwright test e2e/derivatives_underlying_picker_sort.spec.js \
 *     --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { resolve } from 'path';

const PAGE_SRC = resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/routes/(algo)/admin/derivatives/+page.svelte',
);

test.describe('source-grep — underlying picker sort uses qty, not leg count', () => {
  /** @type {string} */
  let src;

  test.beforeAll(() => {
    src = readFileSync(PAGE_SRC, 'utf8');
  });

  // Dimension 3 — Stale-code guard: old variable must be absent.
  test('_rootPosCount is no longer present (stale variable removed)', () => {
    expect(
      src,
      '_rootPosCount must be removed — it accumulated +1 per leg (the bug)',
    ).not.toContain('_rootPosCount');
  });

  // Dimension 1 — SSOT: new variable is declared.
  test('_rootQtySum is declared as the accumulator Map', () => {
    expect(
      src,
      '_rootQtySum must be declared inside underlyingOptionsForPicker',
    ).toContain('const _rootQtySum = new Map()');
  });

  // Dimension 1 — SSOT: accumulation uses abs(qty), not +1.
  test('accumulation uses Math.abs(Number(p.qty || 0)) instead of +1', () => {
    expect(
      src,
      'Must accumulate abs qty per root, not increment by 1',
    ).toContain('(_rootQtySum.get(r) || 0) + Math.abs(Number(p.qty || 0))');
  });

  // Dimension 5 — UX: Tier 1 sort comparator uses _rootQtySum.
  test('Tier 1 (options) sort comparator reads _rootQtySum', () => {
    // Both comparators reference _rootQtySum.get(b) and _rootQtySum.get(a).
    // Count occurrences — expect at least 2 (one for Tier 1, one for Tier 2).
    const hits = (src.match(/_rootQtySum\.get\(/g) || []).length;
    expect(
      hits,
      `_rootQtySum.get( must appear at least 4 times (declaration + both sort comparators use a+b). Found: ${hits}`,
    ).toBeGreaterThanOrEqual(4);
  });

  // Dimension 4 — no new state vars: fix is purely local to the $derived.by block.
  test('_rootQtySum is not a $state variable (stays local to derived)', () => {
    // It should be declared with const inside the block, not with let + $state.
    expect(
      src,
      '_rootQtySum must not be a $state variable',
    ).not.toContain('let _rootQtySum');
    expect(
      src,
      '_rootQtySum must not be a $state variable',
    ).not.toContain('_rootQtySum = $state(');
  });
});
