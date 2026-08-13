/**
 * derivatives_pulse_fallback.spec.js
 *
 * Verifies the pulsePositionsStore fallback in /admin/derivatives.
 *
 * Problem: when Kite REST returns 502 during nightly maintenance (~1am IST),
 * positionsStore.value is null/empty. The page previously planted a
 * provisional NIFTY seed instead of auto-selecting the operator's real
 * position (e.g. CRUDEOIL). pulsePositionsStore (key: md.pulse.positions)
 * is already loaded by the Pulse page and carries the real positions.
 *
 * Fix (three edits in +page.svelte):
 *   1. Import pulsePositionsStore alongside positionsStore.
 *   2. In loadPositions(), use _posSource = positionsStore.value?.length
 *      ? positionsStore.value : (pulsePositionsStore.value ?? [])
 *      so the Pulse store provides positions when the REST poll fails.
 *   3. Provisional NIFTY seed guard now also checks
 *      !(pulsePositionsStore.value?.length) so the seed is only planted
 *      when BOTH stores are empty.
 *
 * Five quality dimensions:
 *  1. SSOT   — pulsePositionsStore import is on the same line as positionsStore.
 *  2. Perf   — _posSource selects positionsStore when non-empty (no overhead).
 *  3. Stale  — grep confirms _posSource variable and the widened seed guard.
 *  4. Reuse  — no new store created; uses the existing pulsePositionsStore.
 *  5. UX     — NIFTY provisional seed fires only when both stores are empty.
 *
 * Because the fallback activates only when positionsStore is empty AND
 * pulsePositionsStore is populated — a state that requires two separate
 * store poll cycles to be in the right phase simultaneously — a live
 * browser test cannot deterministically trigger it without mocking internal
 * Svelte store state. The spec therefore validates the three structural
 * invariants by source-grep (same pattern as derivatives_auto_default.spec.js
 * Test 8 and derivatives_positions_fresh_load.spec.js Test 2/3).
 *
 * Run:
 *   npx playwright test e2e/derivatives_pulse_fallback.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const DERIVATIVES_PAGE = resolve(
  __dirname,
  '../src/routes/(algo)/admin/derivatives/+page.svelte',
);

let _src = '';

test.beforeAll(() => {
  _src = readFileSync(DERIVATIVES_PAGE, 'utf-8');
});

// ── Test 1: SSOT — pulsePositionsStore imported on the same line ─────────────
test('1-SSOT: pulsePositionsStore imported alongside positionsStore', () => {
  // The import must add pulsePositionsStore to the existing marketDataStores line.
  // Confirm both names appear on the SAME import statement (same line).
  const importLine = _src
    .split('\n')
    .find((l) => l.includes("from '$lib/data/marketDataStores.svelte.js'"));

  expect(importLine, 'marketDataStores import line must exist').toBeTruthy();
  expect(importLine).toContain('positionsStore');
  expect(importLine).toContain('pulsePositionsStore');
});

// ── Test 2: Perf — _posSource variable uses the fallback pattern ─────────────
test('2-Perf: _posSource falls back to pulsePositionsStore when positionsStore is empty', () => {
  // The _posSource assignment must be present and reference both stores.
  expect(_src).toContain('const _posSource = positionsStore.value?.length');
  expect(_src).toContain('pulsePositionsStore.value ?? []');
  // The loop must iterate _posSource, not positionsStore.value directly.
  expect(_src).toContain('for (const p of _posSource)');
});

// ── Test 3: Stale — seed guard is widened to cover both stores ───────────────
test('3-Stale: provisional NIFTY seed guard checks both stores', () => {
  // The guard condition must include both positionsStore AND pulsePositionsStore
  // so that the NIFTY seed only fires when both are empty.
  expect(_src).toContain('!(positionsStore.value?.length) && !(pulsePositionsStore.value?.length)');
});

// ── Test 4: Reuse — no new store exported for this feature ───────────────────
test('4-Reuse: no new store definition introduced in +page.svelte', () => {
  // pulsePositionsStore must NOT be defined here — it comes from the shared store module.
  // Confirm the string "createDataStore" does not appear in the derivatives page
  // (stores are defined in marketDataStores.svelte.js, not in the page).
  expect(_src).not.toContain('createDataStore');
  // And confirm the import is from the shared module, not a local definition.
  const importLine = _src
    .split('\n')
    .find((l) => l.includes('pulsePositionsStore'));
  expect(importLine).toContain("from '$lib/data/marketDataStores.svelte.js'");
});

// ── Test 5: UX — positionsStore.value?.length fast-path is still present ─────
test('5-UX: positionsStore fast-path retained; pulsePositionsStore only used as fallback', () => {
  // The ternary must check positionsStore first (non-empty → use it directly).
  // This ensures zero overhead on the normal path where Kite REST is healthy.
  const ternaryBlock = _src.indexOf('const _posSource = positionsStore.value?.length');
  expect(ternaryBlock).toBeGreaterThan(-1);

  // Extract the two lines after the assignment to confirm structure.
  const snippet = _src.slice(ternaryBlock, ternaryBlock + 120);
  expect(snippet).toContain('? positionsStore.value');
  expect(snippet).toContain(': (pulsePositionsStore.value ?? [])');
});
