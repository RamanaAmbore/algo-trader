/**
 * derivatives_auto_select.spec.js
 *
 * Static source-inspection spec — reads the derivatives page source and
 * verifies that all three auto-selection fixes are present:
 *
 *  1. _provisionalSeed $state variable declared
 *  2. _rootQtySum Map built in the position loop and used in Tier 1 + Tier 2 sorts
 *  3. localStorage.setItem for underlying persistence is present
 *  4. localStorage.getItem for underlying restore is present in the onMount block
 *
 * No browser login required — these are source-level assertions.
 *
 * Run:
 *   npx playwright test e2e/derivatives_auto_select.spec.js --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { readFile } from 'fs/promises';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __filename = fileURLToPath(import.meta.url);
const __dir = dirname(__filename);

const DERIV_SRC = join(
  __dir,
  '../src/routes/(algo)/admin/derivatives/+page.svelte',
);

let _src = null;
async function getSrc() {
  if (!_src) _src = await readFile(DERIV_SRC, 'utf8');
  return _src;
}

// ── Check 1: _provisionalSeed state variable ────────────────────────────────
test('_provisionalSeed is declared as a $state(false) variable', async () => {
  const src = await getSrc();
  expect(
    src,
    '_provisionalSeed must be declared as a Svelte 5 $state(false) variable',
  ).toContain('let _provisionalSeed = $state(false)');
});

// ── Check 2a: _rootQtySum Map is declared ───────────────────────────────────
test('_rootQtySum Map is declared alongside _rootPosCount', async () => {
  const src = await getSrc();
  expect(
    src,
    '_rootQtySum must be declared as a new Map()',
  ).toContain('const _rootQtySum = new Map()');
});

// ── Check 2b: _rootQtySum is populated in the positions loop ────────────────
test('_rootQtySum is accumulated in the positions loop using Math.abs(qty)', async () => {
  const src = await getSrc();
  expect(
    src,
    '_rootQtySum.set must accumulate |qty| in the positions loop',
  ).toContain('_rootQtySum.set(r, (_rootQtySum.get(r) || 0) + Math.abs(Number(p.qty ?? 0)))');
});

// ── Check 2c: _rootQtySum is used in Tier 1 sort ───────────────────────────
test('_rootQtySum is used as the primary sort key in the Tier 1 (options) sort', async () => {
  const src = await getSrc();
  // The Tier 1 sort must reference _rootQtySum before _rootPosCount
  const tier1Match = src.match(
    /_rootsWithOptions\]\.sort\(\s*\(a,\s*b\)\s*=>\s*[\s\S]{0,200}?_rootQtySum/,
  );
  expect(
    tier1Match,
    'Tier 1 sort must use _rootQtySum as primary sort key',
  ).toBeTruthy();
});

// ── Check 2d: _rootQtySum is used in Tier 2 sort ───────────────────────────
test('_rootQtySum is used as the primary sort key in the Tier 2 (futures) sort', async () => {
  const src = await getSrc();
  const tier2Match = src.match(
    /_rootsWithFuturesOnly\]\.sort\(\s*\(a,\s*b\)\s*=>\s*[\s\S]{0,200}?_rootQtySum/,
  );
  expect(
    tier2Match,
    'Tier 2 sort must use _rootQtySum as primary sort key',
  ).toBeTruthy();
});

// ── Check 3: localStorage.setItem for underlying ────────────────────────────
test("localStorage.setItem('ramboq.derivatives.underlying') is present", async () => {
  const src = await getSrc();
  expect(
    src,
    "localStorage.setItem must persist the selected underlying",
  ).toContain("localStorage.setItem('ramboq.derivatives.underlying', selectedUnderlying)");
});

// ── Check 4: localStorage.getItem for underlying restore in onMount ─────────
test("localStorage.getItem('ramboq.derivatives.underlying') is present for restore", async () => {
  const src = await getSrc();
  expect(
    src,
    "localStorage.getItem must restore the underlying on mount",
  ).toContain("localStorage.getItem('ramboq.derivatives.underlying')");
});

// ── Check 5: _provisionalSeed is tracked inside the auto-select $effect ─────
test('_provisionalSeed is tracked via void inside the auto-select $effect', async () => {
  const src = await getSrc();
  expect(
    src,
    'void _provisionalSeed must appear inside the auto-select $effect to track it',
  ).toContain('void _provisionalSeed;');
});

// ── Check 6: promote condition uses _provisionalSeed ─────────────────────────
test('promote condition checks _provisionalSeed alongside curIsPromotable', async () => {
  const src = await getSrc();
  expect(
    src,
    'curIsPromotable must combine popular hint check with _provisionalSeed',
  ).toContain("curInOpts?.hint === 'popular' || _provisionalSeed");
});

// ── Check 7: promote only fires on options/futures hints ─────────────────────
test("promote fires only when opts[0] is 'options' or 'futures'", async () => {
  const src = await getSrc();
  expect(
    src,
    "promote condition must gate on opts[0]?.hint === 'options' || opts[0]?.hint === 'futures'",
  ).toContain("opts[0]?.hint === 'options' || opts[0]?.hint === 'futures'");
});

// ── Check 8: _provisionalSeed is cleared after promotion ─────────────────────
test('_provisionalSeed is set to false immediately after promotion fires', async () => {
  const src = await getSrc();
  // After promotion check, _provisionalSeed must be cleared before the untrack call
  const promoteBlock = src.match(
    /curIsPromotable[\s\S]{0,300}?_provisionalSeed\s*=\s*false/,
  );
  expect(
    promoteBlock,
    '_provisionalSeed must be cleared to false inside the promote branch',
  ).toBeTruthy();
});
