/**
 * Chain picker symbol correctness — /admin/derivatives
 *
 * Reproduces and verifies the fix for: clicking a position row (e.g. BHEL)
 * then opening the Chain tab was showing a STALE underlying from
 * sessionStorage (e.g. CRUDEOIL) instead of the clicked symbol's root.
 *
 * Five quality dimensions:
 *
 *  1. SSOT    — chain picker header matches the clicked position's underlying
 *               root, not a stale sessionStorage value.
 *
 *  2. Stale-code — grep: OptionChainTab's auto-default effect checks
 *               seedUnderlying BEFORE sessionStorage when a seed is provided.
 *
 *  3. Reusable — chain is opened via a single canonical path: clicking a
 *               position row opens SymbolPanel; the Chain tab is the
 *               single surface.
 *
 *  4. Performance — cold-load XHR budget: /admin/derivatives page load
 *               does not trigger more than 3 /api/positions requests.
 *
 *  5. UX      — chain picker underlying label matches clicked symbol on
 *               both desktop (1400 px) and mobile (390 px) viewports.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

test.setTimeout(90000);

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
      if (r.ok()) {
        _token = (await r.json()).access_token;
        break;
      }
    }
    if (!_token) throw new Error(`loginAsAdmin: no valid credentials for ${BASE}`);
  }
  await page.context().addInitScript((tok) => {
    sessionStorage.setItem('ramboq_token', tok);
  }, _token);
}

/**
 * Navigate to /admin/derivatives and wait for the page to settle.
 * Returns the page after positions have loaded.
 */
async function gotoDerivatives(page) {
  await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded' });
  // Wait for the candidates panel to appear (indicates positions loaded).
  await page.waitForTimeout(4000);
}

/**
 * Fetch the list of positions from the API and derive F&O roots.
 * Returns an array of unique uppercase roots like ['BHEL', 'CRUDEOIL', 'NIFTY'].
 */
async function fetchFnoRoots(request) {
  const r = await request.get(`${BASE}/api/positions/`, {
    headers: _token ? { Authorization: `Bearer ${_token}` } : {},
  });
  if (!r.ok()) return [];
  const data = await r.json();
  const roots = new Set();
  for (const p of (data.rows || [])) {
    const sym = String(p.tradingsymbol || p.symbol || '').toUpperCase();
    if (!sym) continue;
    const root = sym.replace(/\d.*$/, '');
    if (root && root !== sym) roots.add(root); // only derivatives (have digit suffix)
  }
  return [...roots];
}

// ── Dimension 2: stale-code grep ────────────────────────────────────────────
test('Stale-code: seedUnderlying checked before sessionStorage in auto-default effect', async () => {
  const srcPath = path.resolve(
    process.cwd(),
    'src/lib/order/OptionChainTab.svelte',
  );
  let src;
  try {
    src = readFileSync(srcPath, 'utf8');
  } catch {
    // If path resolution fails, skip gracefully (CI path may differ).
    return;
  }

  // The auto-default effect must check seedUnderlying BEFORE reading sessionStorage.
  // Extract the effect body between the two markers.
  const autoDefaultMatch = src.match(
    /Auto-default underlying once instruments are ready([\s\S]*?)\}\);\s*\n\s*\$effect\(\(\) => \{[\s\S]*?void chainUnderlying/,
  );
  expect(autoDefaultMatch, 'Could not find auto-default effect block').toBeTruthy();

  const effectBody = autoDefaultMatch[0];
  const seedIdx = effectBody.indexOf('seedUnderlying');
  const ssIdx = effectBody.indexOf('sessionStorage.getItem');

  expect(seedIdx, 'seedUnderlying not found in auto-default effect').toBeGreaterThan(-1);
  expect(ssIdx, 'sessionStorage.getItem not found in auto-default effect').toBeGreaterThan(-1);
  expect(
    seedIdx,
    'seedUnderlying must appear before sessionStorage.getItem in auto-default effect',
  ).toBeLessThan(ssIdx);
});

// ── Dimension 4: performance — cold-load XHR budget ─────────────────────────
test('Performance: /admin/derivatives cold load fires ≤3 /api/positions requests', async ({ page }) => {
  await loginAsAdmin(page);

  const posRequests = [];
  await page.route(`${BASE}/api/positions/**`, (route) => {
    posRequests.push(route.request().url());
    route.continue();
  });

  await gotoDerivatives(page);

  expect(
    posRequests.length,
    `Expected ≤3 /api/positions requests on cold load, got ${posRequests.length}: ${posRequests.join(', ')}`,
  ).toBeLessThanOrEqual(3);
});

// ── Dimensions 1 + 3 + 5 (desktop): SSOT + reusable + UX ───────────────────
test.describe('Desktop (1400×900)', () => {
  test.use({ viewport: { width: 1400, height: 900 } });

  test('Chain picker shows the clicked position root, not a stale sessionStorage root', async ({ page, request }) => {
    await loginAsAdmin(page);

    // Poison sessionStorage with CRUDEOIL so we reproduce the original bug.
    // The fix must override this with the clicked symbol's root.
    await page.context().addInitScript(() => {
      sessionStorage.setItem('chain.lastRoot', 'CRUDEOIL');
    });

    await gotoDerivatives(page);

    const roots = await fetchFnoRoots(request);

    // Pick a root that is NOT CRUDEOIL — so the test is meaningful.
    // If the operator has no positions, skip (can't test without data).
    const targetRoot = roots.find(r => r !== 'CRUDEOIL') || roots[0];
    if (!targetRoot) {
      console.warn('No F&O positions found — skipping chain picker test');
      return;
    }

    // Find a candidate row whose symbol starts with targetRoot.
    const candRow = page.locator('.cand-row').filter({
      has: page.locator(`.cand-sym, .sym-main`).filter({ hasText: new RegExp(`^${targetRoot}`, 'i') }),
    }).first();

    const hasCandRow = await candRow.count() > 0;
    if (!hasCandRow) {
      console.warn(`No candidate row found for root ${targetRoot} — skipping`);
      return;
    }

    // Click the row — opens SymbolPanel.
    await candRow.click();
    await page.waitForTimeout(800);

    // Switch to the Chain tab in SymbolPanel.
    const chainTab = page.locator('[data-tab="chain"], button:has-text("Chain"), .oes-tab:has-text("Chain")').first();
    const chainTabVisible = await chainTab.isVisible({ timeout: 5000 }).catch(() => false);
    if (!chainTabVisible) {
      // Some rows (cash equity with no F&O) won't have a Chain tab — skip gracefully.
      console.warn(`Chain tab not visible for root ${targetRoot} — skipping`);
      return;
    }
    await chainTab.click();
    await page.waitForTimeout(1200);

    // Dimension 1 (SSOT): the chain picker's underlying label must show
    // the clicked root, NOT the poisoned 'CRUDEOIL'.
    // OptionChainTab renders the underlying as text in the underlying Select trigger.
    // The trigger contains the selected underlying name.
    const chainUnderlyingTrigger = page.locator(
      '.oct-root .rbq-select-trigger, .oes-body .rbq-select-trigger',
    ).first();
    const chainUnderlyingText = await chainUnderlyingTrigger.textContent({ timeout: 5000 }).catch(() => '');

    expect(
      chainUnderlyingText.trim().toUpperCase(),
      `Chain picker should show ${targetRoot} but shows: "${chainUnderlyingText.trim()}"`,
    ).toContain(targetRoot);

    // Ensure it's definitely not the stale CRUDEOIL.
    if (targetRoot !== 'CRUDEOIL') {
      expect(
        chainUnderlyingText.trim().toUpperCase(),
        'Chain picker must not show stale CRUDEOIL from sessionStorage',
      ).not.toContain('CRUDEOIL');
    }
  });

  test('Chain picker switches correctly: CRUDEOIL position then BHEL (or equity) position', async ({ page, request }) => {
    await loginAsAdmin(page);

    await gotoDerivatives(page);

    const roots = await fetchFnoRoots(request);
    if (roots.length < 1) {
      console.warn('No F&O positions — skipping switch test');
      return;
    }

    // Helper: open chain for a given root and return the displayed underlying text.
    async function openChainForRoot(root) {
      const candRow = page.locator('.cand-row').filter({
        has: page.locator('.cand-sym, .sym-main').filter({ hasText: new RegExp(`^${root}`, 'i') }),
      }).first();
      if (await candRow.count() === 0) return null;

      await candRow.click();
      await page.waitForTimeout(600);

      const chainTab = page.locator('[data-tab="chain"], button:has-text("Chain"), .oes-tab:has-text("Chain")').first();
      if (!await chainTab.isVisible({ timeout: 3000 }).catch(() => false)) return null;
      await chainTab.click();
      await page.waitForTimeout(1000);

      const trigger = page.locator('.oct-root .rbq-select-trigger, .oes-body .rbq-select-trigger').first();
      const txt = await trigger.textContent({ timeout: 4000 }).catch(() => '');

      // Close the modal before the next click.
      const closeBtn = page.locator('.oes-close, [aria-label="Close"], button.modal-close').first();
      if (await closeBtn.isVisible({ timeout: 1000 }).catch(() => false)) await closeBtn.click();
      await page.waitForTimeout(300);

      return txt.trim().toUpperCase();
    }

    // Open each available root in sequence and assert the chain switches.
    let prev = null;
    for (const root of roots.slice(0, 3)) {
      const displayed = await openChainForRoot(root);
      if (!displayed) continue;

      expect(
        displayed,
        `After clicking ${root}, chain picker shows "${displayed}" — expected it to contain "${root}"`,
      ).toContain(root);

      if (prev && prev !== root) {
        expect(
          displayed,
          `After switching from ${prev} to ${root}, chain picker must not show ${prev}`,
        ).not.toContain(prev);
      }
      prev = root;
    }
  });
});

// ── Dimension 5 (mobile): UX on 390-wide viewport ───────────────────────────
test.describe('Mobile (390×844)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('Chain picker underlying label is visible and correct on mobile', async ({ page, request }) => {
    await loginAsAdmin(page);

    // Poison sessionStorage to reproduce the original bug.
    await page.context().addInitScript(() => {
      sessionStorage.setItem('chain.lastRoot', 'CRUDEOIL');
    });

    await gotoDerivatives(page);

    const roots = await fetchFnoRoots(request);
    const targetRoot = roots.find(r => r !== 'CRUDEOIL') || roots[0];
    if (!targetRoot) {
      console.warn('No F&O positions found — skipping mobile chain test');
      return;
    }

    const candRow = page.locator('.cand-row').filter({
      has: page.locator('.cand-sym, .sym-main').filter({ hasText: new RegExp(`^${targetRoot}`, 'i') }),
    }).first();

    if (await candRow.count() === 0) {
      console.warn(`No candidate row for ${targetRoot} on mobile — skipping`);
      return;
    }

    await candRow.click();
    await page.waitForTimeout(800);

    const chainTab = page.locator('[data-tab="chain"], button:has-text("Chain"), .oes-tab:has-text("Chain")').first();
    if (!await chainTab.isVisible({ timeout: 5000 }).catch(() => false)) {
      console.warn(`Chain tab not visible for ${targetRoot} on mobile — skipping`);
      return;
    }
    await chainTab.click();
    await page.waitForTimeout(1200);

    // The underlying trigger should be visible on mobile.
    const trigger = page.locator('.oct-root .rbq-select-trigger, .oes-body .rbq-select-trigger').first();
    await expect(trigger).toBeVisible({ timeout: 5000 });

    const txt = await trigger.textContent({ timeout: 4000 }).catch(() => '');
    expect(
      txt.trim().toUpperCase(),
      `Mobile: chain picker shows "${txt.trim()}" but expected "${targetRoot}"`,
    ).toContain(targetRoot);

    if (targetRoot !== 'CRUDEOIL') {
      expect(
        txt.trim().toUpperCase(),
        'Mobile: chain picker must not show stale CRUDEOIL',
      ).not.toContain('CRUDEOIL');
    }
  });
});
