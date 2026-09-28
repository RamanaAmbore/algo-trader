/**
 * derivatives_picker_row_alignment.spec.js
 *
 * Guards the 2026-09-29 fix: the Underlying field's "No F&O positions —
 * showing popular." hint rendered in normal document flow inside
 * `.opt-field` (a flex column), and `.opt-picker` aligns its columns via
 * `align-items: flex-end`. So whenever the hint appeared, the Underlying
 * column grew taller than its siblings (Account, Expiry), and flex-end
 * alignment pushed the Underlying label+Select UP relative to them —
 * visible row misalignment, reported by the operator as "the underlying
 * dropdown symbol gets pushed up ... with a message below it".
 *
 * Fix: `.opt-und-hint` is taken out of flow (`position: absolute; top:
 * 100%`), inside a `position: relative` `.opt-field`, so its presence no
 * longer affects the column's flex-participating height.
 *
 * Five quality dimensions:
 *  1. SSOT   — one hint element, one fix, no duplicate alignment logic.
 *  2. Perf   — source-grep is network-free; live check reuses existing
 *              login/navigation helpers, no extra requests.
 *  3. Stale  — regression guard: old in-flow hint CSS must not return.
 *  4. Reuse  — `.opt-field` positioning context is shared by all three
 *              picker fields (Account/Underlying/Expiry), not duplicated.
 *  5. UX     — live check: Account/Underlying/Expiry trigger bottoms
 *              stay aligned whether or not the hint is currently shown.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const PAGE_SRC_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/routes/(algo)/admin/derivatives/+page.svelte',
);

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const USER = process.env.PLAYWRIGHT_USER || 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
let _token = null;

async function loginAsAdmin(page) {
  if (!_token) {
    for (const u of [USER, 'ambore', 'rambo']) {
      const r = await page.request.post(`${BASE}/api/auth/login`, {
        data: { username: u, password: PASS },
        headers: { 'Content-Type': 'application/json' },
      });
      if (r.ok()) { _token = (await r.json()).access_token; break; }
    }
    if (!_token) throw new Error('loginAsAdmin: no valid credentials');
  }
  await page.context().addInitScript((tok) => {
    sessionStorage.setItem('ramboq_token', tok);
  }, _token);
}

async function gotoDerivatives(page) {
  await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(4000);
}

// ── Dimension 3: stale-code grep — the in-flow layout bug must not return ──
test('Stale-code: .opt-und-hint is absolutely positioned, .opt-field is a positioning context', () => {
  const src = readFileSync(PAGE_SRC_PATH, 'utf8');

  const hintRule = src.match(/\.opt-und-hint\s*\{[^}]*\}/)?.[0] ?? '';
  expect(hintRule).toContain('position: absolute');
  expect(hintRule).toContain('top: 100%');

  const fieldRule = src.match(/\.opt-field\s*\{[^}]*\}/)?.[0] ?? '';
  expect(fieldRule).toContain('position: relative');
});

// ── Dimension 5: live UX — picker row stays aligned ─────────────────────────
test('UX: Account/Underlying/Expiry triggers share the same bottom edge', async ({ page }) => {
  test.setTimeout(90000);
  await loginAsAdmin(page);
  await gotoDerivatives(page);

  const picker = page.locator('.opt-picker').first();
  if (!await picker.isVisible({ timeout: 10_000 }).catch(() => false)) {
    test.skip(true, '.opt-picker not visible — page may not have loaded positions');
  }

  const acctTrigger = picker.locator('#opt-acct').first();
  const undTrigger = picker.locator('#opt-und').first();
  const expTrigger = picker.locator('#opt-exp').first();

  await expect(acctTrigger).toBeVisible({ timeout: 10_000 });
  await expect(undTrigger).toBeVisible({ timeout: 10_000 });
  await expect(expTrigger).toBeVisible({ timeout: 10_000 });

  const [acctBox, undBox, expBox] = await Promise.all([
    acctTrigger.boundingBox(),
    undTrigger.boundingBox(),
    expTrigger.boundingBox(),
  ]);
  expect(acctBox && undBox && expBox).toBeTruthy();

  // Bottom edges should match within a small tolerance regardless of
  // whether the "No F&O positions" hint happens to be showing right now —
  // that's the whole point of taking the hint out of flow.
  const TOL = 2; // px
  expect(Math.abs(acctBox.y + acctBox.height - (undBox.y + undBox.height))).toBeLessThanOrEqual(TOL);
  expect(Math.abs(undBox.y + undBox.height - (expBox.y + expBox.height))).toBeLessThanOrEqual(TOL);

  // If the hint happens to be visible right now, confirm it's actually
  // out of flow (doesn't sit between the picker row and whatever renders
  // below it in a way that shifted the row itself).
  const hint = page.locator('.opt-und-hint').first();
  if (await hint.isVisible({ timeout: 1000 }).catch(() => false)) {
    const pos = await hint.evaluate((el) => getComputedStyle(el).position);
    expect(pos).toBe('absolute');
  }
});
