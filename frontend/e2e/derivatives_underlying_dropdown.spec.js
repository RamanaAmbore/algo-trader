/**
 * Underlying dropdown freshness — /admin/derivatives
 *
 * Verifies five quality dimensions:
 *
 *  1. SSOT          — options shown in the dropdown match the set of
 *                     underlyings from /api/positions (no permanent drift).
 *
 *  2. Performance   — /api/positions is called with fresh=true on mount
 *                     (backend 30s TTL bypassed) so positions updated while
 *                     the operator was on a different page appear immediately.
 *
 *  3. Stale-code    — old "$bookChanged in $effect" pattern replaced by
 *                     explicit subscribe bridge; observable indirectly by
 *                     verifying the page still uses #opt-und canonical Select.
 *
 *  4. Reusable      — underlying picker is #opt-und (Select button), expiry
 *                     is #opt-exp (MultiSelect), account is #opt-acct.
 *
 *  5. UX (mobile+desktop) — dropdown trigger visible at 360 and 1400 px.
 *
 * DOM structure note:
 *   Select.svelte renders: <div class="rbq-select"> > <button id="{id}" class="rbq-select-trigger">
 *   So #opt-und IS the trigger button itself. The panel is a sibling of the
 *   button inside .rbq-select, not a child of #opt-und.
 *   Locator helpers use `#opt-und.rbq-select-trigger` for the button and
 *   `#opt-und ~ .rbq-select-panel, #opt-und + .rbq-select-panel` for the panel
 *   (or just navigate to the parent .rbq-select wrapper).
 */

import { test, expect } from '@playwright/test';

test.setTimeout(60000);

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const USER = process.env.PLAYWRIGHT_USER || 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

/** Shared token cache so login only happens once per worker. */
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
  // Inject token before navigation so the page sees it on first load.
  await page.context().addInitScript((tok) => {
    sessionStorage.setItem('ramboq_token', tok);
  }, _token);
}

/**
 * Navigate to /admin/derivatives and wait for the #opt-und trigger to appear.
 *
 * Select.svelte places the `id` prop on the <button class="rbq-select-trigger">
 * element — so #opt-und IS the trigger button, not a parent wrapper.
 * Returns the trigger button locator directly.
 */
async function gotoDerivatives(page) {
  await page.goto(`${BASE}/admin/derivatives`, { waitUntil: 'domcontentloaded' });
  // #opt-und is the <button class="rbq-select-trigger"> (id lives on the button).
  const trigger = page.locator('#opt-und');
  await trigger.waitFor({ state: 'visible', timeout: 25_000 });
  return trigger;
}

/**
 * Return the .rbq-select-panel that belongs to a given trigger button.
 * The panel is a sibling rendered by the parent <div class="rbq-select">,
 * so we navigate up to the wrapper then down to the panel.
 */
function getPanelForTrigger(triggerLocator) {
  // triggerLocator is the <button id="opt-und">.
  // Parent is <div class="rbq-select">, panel is a child of that div.
  return triggerLocator.locator('xpath=..').locator('.rbq-select-panel');
}

// ── Dimension 1 + 2: SSOT + fresh=true on mount ──────────────────────────
test('SSOT — dropdown matches /api/positions roots; mount calls fresh=true', async ({ page, request }) => {
  await loginAsAdmin(page);

  // Intercept /api/positions calls to verify fresh=true is sent on mount.
  // Set up BEFORE navigation so no early calls are missed.
  const posRequests = [];
  await page.route(`${BASE}/api/positions/**`, (route) => {
    posRequests.push(route.request().url());
    route.continue();
  });

  const trigger = await gotoDerivatives(page);

  // Allow the async loadPositions to settle (includes network round-trip).
  await page.waitForTimeout(4000);

  // ── Dimension 2 / 3: fresh=true on mount ────────────────────────────
  // loadPositions({ fresh: true }) on mount sends ?fresh=1 to bypass
  // the backend 30s TTL cache. Confirm at least one call used it.
  const hasFreshMount = posRequests.some(url => url.includes('fresh=1'));
  expect(
    hasFreshMount,
    `Expected /api/positions/?fresh=1 on mount. Seen: ${posRequests.join(', ')}`
  ).toBe(true);

  // Exactly ONE fresh call on mount (no double-fetch from bookChanged
  // firing at counter=0 on subscribe).
  const freshCount = posRequests.filter(u => u.includes('fresh=1')).length;
  expect(
    freshCount,
    `Expected exactly 1 fresh=1 call on mount, got ${freshCount}`
  ).toBe(1);

  // ── Dimension 1: SSOT — dropdown options ⊇ API derivative roots ─────
  // Fetch ground-truth positions from the API.
  const posR = await request.get(`${BASE}/api/positions/`, {
    headers: _token ? { Authorization: `Bearer ${_token}` } : {},
  });
  const posData = posR.ok() ? await posR.json() : {};
  const expectedRoots = new Set();
  for (const p of (posData.rows || [])) {
    const sym = p.tradingsymbol || p.symbol || '';
    if (!/(CE|PE|FUT)$/i.test(sym)) continue;
    const root = sym.toUpperCase().replace(/\d.*$/, '');
    if (root) expectedRoots.add(root);
  }

  if (expectedRoots.size > 0) {
    // Open the picker to inspect the rendered option list.
    await trigger.click();
    const panel = getPanelForTrigger(trigger);
    await panel.waitFor({ state: 'visible', timeout: 5000 });

    const optTexts = await panel.locator('.rbq-select-option-label').allTextContents();
    const rendered = new Set(optTexts.map(t => t.trim().toUpperCase()).filter(Boolean));

    for (const root of expectedRoots) {
      expect(
        rendered.has(root),
        `Underlying "${root}" from /api/positions missing in dropdown (rendered: ${[...rendered].join(', ')})`
      ).toBe(true);
    }
    await page.keyboard.press('Escape');
  } else {
    // No derivative positions — trigger shows placeholder text.
    const triggerText = await trigger.textContent();
    expect(triggerText).toMatch(/no options in book|pick underlying/i);
  }
});

// ── Dimension 4: Reusable canonical component usage ───────────────────────
test('Canonical picker IDs: #opt-und, #opt-exp, #opt-acct all present', async ({ page }) => {
  await loginAsAdmin(page);
  await gotoDerivatives(page);
  // #opt-und is already visible (gotoDerivatives guarantee).
  // #opt-exp and #opt-acct are MultiSelect buttons — same id-on-button pattern.
  await expect(page.locator('#opt-und')).toBeVisible({ timeout: 5_000 });
  await expect(page.locator('#opt-exp')).toBeVisible({ timeout: 5_000 });
  await expect(page.locator('#opt-acct')).toBeVisible({ timeout: 5_000 });
});

// ── Dimension 5a: UX — desktop viewport (1400×900) ───────────────────────
test('Desktop 1400×900: underlying trigger visible and openable', async ({ page }) => {
  await page.setViewportSize({ width: 1400, height: 900 });
  await loginAsAdmin(page);
  const trigger = await gotoDerivatives(page);

  // gotoDerivatives() already confirmed the trigger is visible.
  await expect(trigger).toBeVisible();

  await trigger.click();
  const panel = getPanelForTrigger(trigger);
  await panel.waitFor({ state: 'visible', timeout: 5000 });
  await expect(panel).toBeVisible();
  await page.keyboard.press('Escape');
});

// ── Dimension 5b: UX — mobile portrait (360×800) ─────────────────────────
test('Mobile 360×800: underlying trigger visible', async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await loginAsAdmin(page);
  const trigger = await gotoDerivatives(page);

  // gotoDerivatives() already confirmed the trigger is visible.
  await expect(trigger).toBeVisible();
});
