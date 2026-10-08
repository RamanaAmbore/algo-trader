// Admin Settings page: Global Switches panel (PATCH /api/admin/global-switches).
// paper_trading_mode is a real risk switch (prod-wide, every account) — gets
// an explicit danger-styled warning + ConfirmModal gate. default_agent_trade_mode
// is a softer per-create default, saved directly via Select.
//
// 2026-10: four previously-unexposed GlobalSwitchesResponse fields get their
// first UI here — expiry_close_hold_enabled / template_exit_hold_enabled
// (review-hold booleans, danger-confirm-gated on the OFF/"auto" direction,
// same treatment as paper_trading_mode) and expiry_close_lead_minutes_mcx/nfo
// (numeric lead-time inputs). Source-level guards read the real page source +
// api.js wrapper; the live-browser block drives the actual DOM.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { loginAsAdmin } from './fixtures/auth.js';

const pageSrc = readFileSync(
  new URL('../src/routes/(algo)/admin/settings/+page.svelte', import.meta.url).pathname, 'utf8'
);
const apiSrc = readFileSync(
  new URL('../src/lib/api.js', import.meta.url).pathname, 'utf8'
);

test.describe('admin/settings — Global Switches panel', () => {
  test('api.js exposes a PATCH wrapper following the existing _patch convention', () => {
    expect(apiSrc).toMatch(/export const updateGlobalSwitches = \(payload\) =>\s*\n\s*_patch\('\/admin\/global-switches', payload, \{ auth: true \}\);/);
  });

  test('api.js exposes the renderer-list GET for the automation builder', () => {
    expect(apiSrc).toMatch(/export const fetchAgentRenderers = \(\) => _get\('\/agents\/renderers', \{ auth: true \}\);/);
  });

  test('panel section exists, gated the same way as the rest of the settings page', () => {
    expect(pageSrc).toMatch(/<h3 class="section-heading">Global Switches<\/h3>/);
  });

  test('paper_trading_mode shows an explicit prod-wide risk warning', () => {
    const section = pageSrc.slice(
      pageSrc.indexOf('Global Switches —'),
      pageSrc.indexOf('{#if execRows.length}')
    );
    expect(section).toMatch(/font-mono text-\[var\(--algo-sky\)\]">paper_trading_mode</);
    expect(section).toMatch(/Affects every account, prod-wide — flips real-money execution\./);
  });

  test('Global Switches panel uses design tokens, not off-palette literals', () => {
    const section = pageSrc.slice(
      pageSrc.indexOf('Global Switches —'),
      pageSrc.indexOf('{#if execRows.length}')
    );
    // No raw hex literal for the sky accent anywhere in this panel.
    expect(section).not.toMatch(/text-\[#7dd3fc\]/);
    // No Tailwind emerald/red utility classes — must route through the
    // --algo-green / --algo-red tokens like the rest of the page.
    expect(section).not.toMatch(/bg-emerald-500\/15 text-emerald-300/);
    expect(section).not.toMatch(/bg-red-500\/20 text-red-300/);
    expect(section).toMatch(/bg-\[var\(--algo-green\)\]\/15 text-\[var\(--algo-green\)\]/);
    expect(section).toMatch(/bg-\[var\(--algo-red\)\]\/20 text-\[var\(--algo-red\)\]/);
  });

  test('paper_trading_mode flip is gated behind ConfirmModal with danger:true', () => {
    expect(pageSrc).toMatch(/<ConfirmModal bind:this=\{_globalSwitchConfirmRef\} \/>/);
    const fn = pageSrc.slice(
      pageSrc.indexOf('async function toggleGlobalPaperMode()'),
      pageSrc.indexOf('async function toggleGlobalPaperMode()') + 900
    );
    expect(fn).toMatch(/_globalSwitchConfirmRef\?\.ask\(\{/);
    expect(fn).toMatch(/danger: true,/);
    expect(fn).toMatch(/await updateGlobalSwitches\(\{ paper_trading_mode: next \}\);/);
  });

  test('default_agent_trade_mode reads its current value from the canonical GET, writes via PATCH', () => {
    expect(pageSrc).toMatch(/value=\{globalSwitches\?\.default_agent_trade_mode \|\| 'paper'\}/);
    expect(pageSrc).toMatch(/await updateGlobalSwitches\(\{ default_agent_trade_mode: v \}\);/);
  });

  test('globalSwitches state is sourced from the real GET /admin/global-switches, not inferred', () => {
    expect(apiSrc).toMatch(/export const fetchGlobalSwitches = \(\) => _get\('\/admin\/global-switches', \{ auth: true \}\);/);
    expect(pageSrc).toMatch(/globalSwitches = await fetchGlobalSwitches\(\);/);
    // Both mutators re-sync local state from the PATCH response so the
    // panel never drifts from the server's own post-write truth.
    expect(pageSrc).toMatch(/const updated = await updateGlobalSwitches\(\{ paper_trading_mode: next \}\);\s*\n\s*globalSwitches = updated;/);
  });

  // ── Expiry-close / template-exit hold rows (2026-10) ────────────────
  // Four previously-unexposed GlobalSwitchesResponse fields finally get a
  // UI: two review-hold booleans (danger-confirm-gated on the OFF/auto
  // direction, same as paper_trading_mode) and two MCX/NFO lead-time
  // numeric inputs. The human-readable label is the primary visible text
  // (not the raw snake_case key) — the key is kept as small secondary
  // mono text, matching the task's "operator confusion" complaint.

  test('expiry_close_hold_enabled row has an operator-facing label, a Held/Auto pill, and a flip button', () => {
    expect(pageSrc).toMatch(/<span>Hold expiry-close orders for review<\/span>/);
    expect(pageSrc).toMatch(/font-mono text-\[length:var\(--fs-2xs\)\] opacity-50">expiry_close_hold_enabled</);
    expect(pageSrc).toMatch(/globalSwitches\?\.expiry_close_hold_enabled \? 'HELD' : 'AUTO'/);
    expect(pageSrc).toMatch(/onclick=\{toggleExpiryCloseHold\}/);
  });

  test('template_exit_hold_enabled row has an operator-facing label, a Held/Auto pill, and a flip button', () => {
    expect(pageSrc).toMatch(/<span>Hold template exit GTTs after fill<\/span>/);
    expect(pageSrc).toMatch(/font-mono text-\[length:var\(--fs-2xs\)\] opacity-50">template_exit_hold_enabled</);
    expect(pageSrc).toMatch(/globalSwitches\?\.template_exit_hold_enabled \? 'HELD' : 'AUTO'/);
    expect(pageSrc).toMatch(/onclick=\{toggleTemplateExitHold\}/);
  });

  test('both hold toggles carry an InfoHint explaining the ON default', () => {
    expect(pageSrc).toMatch(/InfoHint popup panel title="expiry_close_hold_enabled" text="ON \(default\) holds automated expiry-close orders/);
    expect(pageSrc).toMatch(/InfoHint popup panel title="template_exit_hold_enabled" text="ON \(default\) holds Bracket\/template exit GTTs/);
  });

  test('disabling either hold is danger-confirm-gated; enabling is not; both resync the generic catalog', () => {
    const expiryFn = pageSrc.slice(
      pageSrc.indexOf('async function toggleExpiryCloseHold()'),
      pageSrc.indexOf('async function toggleTemplateExitHold()')
    );
    expect(expiryFn).toMatch(/if \(!next\) \{/);
    expect(expiryFn).toMatch(/_globalSwitchConfirmRef\?\.ask\(\{/);
    expect(expiryFn).toMatch(/danger: true,/);
    expect(expiryFn).toMatch(/⚠ Expiry closes will fire automatically with no review\./);
    expect(expiryFn).toMatch(/await updateGlobalSwitches\(\{ expiry_close_hold_enabled: next \}\);/);
    // Mirrors saveDefaultAgentTradeMode's resync so the generic settings
    // catalog (same flag under its inverted hold.*_released storage key)
    // never drifts from this panel.
    expect(expiryFn).toMatch(/await load\(\);/);

    const templateFn = pageSrc.slice(
      pageSrc.indexOf('async function toggleTemplateExitHold()'),
      pageSrc.indexOf('function _validLeadMinutes(')
    );
    expect(templateFn).toMatch(/if \(!next\) \{/);
    expect(templateFn).toMatch(/_globalSwitchConfirmRef\?\.ask\(\{/);
    expect(templateFn).toMatch(/danger: true,/);
    expect(templateFn).toMatch(/⚠ Template exits will attach automatically with no review\./);
    expect(templateFn).toMatch(/await updateGlobalSwitches\(\{ template_exit_hold_enabled: next \}\);/);
    expect(templateFn).toMatch(/await load\(\);/);
  });

  test('lead-time save handlers reject empty/out-of-range input instead of silently PATCHing 0', () => {
    expect(pageSrc).toMatch(/function _validLeadMinutes\(/);
    expect(pageSrc).toMatch(/if \(raw === '' \|\| raw == null\) return null;/);
    expect(pageSrc).toMatch(/if \(!Number\.isInteger\(n\) \|\| n < 0 \|\| n > max\) return null;/);

    const mcxFn = pageSrc.slice(
      pageSrc.indexOf('async function saveExpiryCloseLeadMcx('),
      pageSrc.indexOf('async function saveExpiryCloseLeadNfo(')
    );
    expect(mcxFn).toMatch(/_validLeadMinutes\(el\.value, 180\)/);
    expect(mcxFn).toMatch(/if \(n == null\) \{/);
    expect(mcxFn).toMatch(/el\.value = String\(globalSwitches\?\.expiry_close_lead_minutes_mcx \?\? 30\);/);
    expect(mcxFn).toMatch(/await updateGlobalSwitches\(\{ expiry_close_lead_minutes_mcx: n \}\);/);
  });

  test('expiry_close_lead_minutes_mcx renders a 0-180 numeric input with an operator-facing label', () => {
    expect(pageSrc).toMatch(/<span>MCX expiry-close lead time \(minutes\)<\/span>/);
    expect(pageSrc).toMatch(/font-mono text-\[length:var\(--fs-2xs\)\] opacity-50">expiry_close_lead_minutes_mcx</);
    expect(pageSrc).toMatch(/aria-label="MCX expiry-close lead time \(minutes\)"/);
    expect(pageSrc).toMatch(/min=\{0\} max=\{180\} step=\{1\}/);
    expect(pageSrc).toMatch(/onchange=\{\(e\) => saveExpiryCloseLeadMcx\(e\.currentTarget\)\}/);
  });

  test('expiry_close_lead_minutes_nfo renders a 0-120 numeric input with an operator-facing label', () => {
    expect(pageSrc).toMatch(/<span>NFO expiry-close lead time \(minutes\)<\/span>/);
    expect(pageSrc).toMatch(/font-mono text-\[length:var\(--fs-2xs\)\] opacity-50">expiry_close_lead_minutes_nfo</);
    expect(pageSrc).toMatch(/aria-label="NFO expiry-close lead time \(minutes\)"/);
    expect(pageSrc).toMatch(/min=\{0\} max=\{120\} step=\{1\}/);
    expect(pageSrc).toMatch(/onchange=\{\(e\) => saveExpiryCloseLeadNfo\(e\.currentTarget\)\}/);
    expect(pageSrc).toMatch(/await updateGlobalSwitches\(\{ expiry_close_lead_minutes_nfo: n \}\);/);
  });
});

// ── Live browser — drives the real DOM against a mocked
// /api/admin/global-switches. The PATCH branch is ALWAYS fulfilled by the
// mock (never route.continue()) so a regression in the confirm-gate can
// never send a real PATCH that flips a shared dev/prod flag.
test.describe('admin/settings — Global Switches panel (live browser)', () => {
  const GET_BODY = {
    paper_trading_mode: true,
    default_agent_trade_mode: 'paper',
    expiry_close_hold_enabled: true,
    template_exit_hold_enabled: true,
    expiry_close_lead_minutes_mcx: 30,
    expiry_close_lead_minutes_nfo: 15,
  };

  /** Skips the test if the logged-in user lacks manage_settings — the page
   *  renders an access-denied EmptyState instead of the panel for anyone
   *  else, same conditional guard settings_page_access.spec.js uses.
   *  Passes the Authorization header explicitly — page.request does not
   *  automatically inherit context.setExtraHTTPHeaders() (that header
   *  injection targets browser-page network traffic, not the Node-side
   *  APIRequestContext), same reasoning settings_page_access.spec.js's
   *  own explicit-header calls document. */
  async function skipUnlessCanManageSettings(page, token) {
    const res = await page.request.get('/api/auth/whoami', {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok()) { test.skip(true, 'whoami failed — cannot verify manage_settings cap'); return; }
    const caps = (await res.json())?.caps || [];
    test.skip(!caps.includes('manage_settings'), 'test user lacks manage_settings — page shows access-denied');
  }

  /** Mocks GET (always GET_BODY merged with any override) and PATCH
   *  (always fulfilled locally — recorded into `patches`, never forwarded
   *  to the real backend) for /api/admin/global-switches. */
  async function mockGlobalSwitches(page, patches, getOverride = {}) {
    await page.route('**/api/admin/global-switches', async (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ ...GET_BODY, ...getOverride }),
        });
      }
      const body = route.request().postDataJSON();
      patches.push(body);
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...GET_BODY, ...getOverride, ...body }),
      });
    });
  }

  for (const row of [
    { toggleKey: 'expiry_close_hold_enabled', flipLabel: 'Hold expiry-close orders for review',
      dangerMsg: '⚠ Expiry closes will fire automatically with no review.' },
    { toggleKey: 'template_exit_hold_enabled', flipLabel: 'Hold template exit GTTs after fill',
      dangerMsg: '⚠ Template exits will attach automatically with no review.' },
  ]) {
    test(`flipping "${row.flipLabel}" OFF opens the danger confirm modal before any PATCH fires; Cancel sends none, Confirm sends one`, async ({ page }) => {
      const { token } = await loginAsAdmin(page);
      await skipUnlessCanManageSettings(page, token);

      /** @type {any[]} */
      const patches = [];
      await mockGlobalSwitches(page, patches);

      await page.goto('/admin/settings');
      const label = page.locator('.settings-row', { hasText: row.flipLabel });
      await label.waitFor({ state: 'visible', timeout: 15000 });
      const flipBtn = label.getByRole('button', { name: /Flip to AUTO/ });
      await flipBtn.click();

      // Confirm modal must appear with the danger wording before any PATCH
      // request is allowed to fire.
      await expect(page.getByText(row.dangerMsg)).toBeVisible({ timeout: 5000 });
      expect(patches.length).toBe(0);

      // Cancel — no PATCH must fire.
      await page.getByRole('button', { name: 'Cancel' }).click();
      await expect(page.getByText(row.dangerMsg)).not.toBeVisible();
      expect(patches.length).toBe(0);

      // Re-open and Confirm — exactly one PATCH, flipping this key to false.
      await flipBtn.click();
      await expect(page.getByText(row.dangerMsg)).toBeVisible({ timeout: 5000 });
      await page.getByRole('button', { name: 'Disable hold' }).click();

      await expect.poll(() => patches.length).toBe(1);
      expect(patches[0]).toEqual({ [row.toggleKey]: false });
    });
  }

  test('turning a hold back ON needs no confirm and PATCHes immediately', async ({ page }) => {
    const { token } = await loginAsAdmin(page);
    await skipUnlessCanManageSettings(page, token);

    /** @type {any[]} */
    const patches = [];
    await mockGlobalSwitches(page, patches, { expiry_close_hold_enabled: false });

    await page.goto('/admin/settings');
    const label = page.locator('.settings-row', { hasText: 'Hold expiry-close orders for review' });
    await label.waitFor({ state: 'visible', timeout: 15000 });
    const flipBtn = label.getByRole('button', { name: /Flip to HELD/ });
    await flipBtn.click();

    // Safe direction — no confirm dialog, PATCH fires right away.
    await expect.poll(() => patches.length).toBe(1);
    expect(patches[0]).toEqual({ expiry_close_hold_enabled: true });
  });

  test('numeric lead-time inputs accept an in-range value and PATCH it', async ({ page }) => {
    const { token } = await loginAsAdmin(page);
    await skipUnlessCanManageSettings(page, token);

    /** @type {any[]} */
    const patches = [];
    await mockGlobalSwitches(page, patches);

    await page.goto('/admin/settings');
    const mcxInput = page.getByLabel('MCX expiry-close lead time (minutes)');
    await mcxInput.waitFor({ state: 'visible', timeout: 15000 });
    await mcxInput.fill('45');
    await mcxInput.blur();

    await expect.poll(() => patches.length).toBe(1);
    expect(patches[0]).toEqual({ expiry_close_lead_minutes_mcx: 45 });
    await expect(mcxInput).toHaveValue('45');

    const nfoInput = page.getByLabel('NFO expiry-close lead time (minutes)');
    await nfoInput.fill('20');
    await nfoInput.blur();
    await expect.poll(() => patches.length).toBe(2);
    expect(patches[1]).toEqual({ expiry_close_lead_minutes_nfo: 20 });
  });

  test('clearing a lead-time input sends no PATCH and resets to the last-known value', async ({ page }) => {
    const { token } = await loginAsAdmin(page);
    await skipUnlessCanManageSettings(page, token);

    /** @type {any[]} */
    const patches = [];
    await mockGlobalSwitches(page, patches);

    await page.goto('/admin/settings');
    const mcxInput = page.getByLabel('MCX expiry-close lead time (minutes)');
    await mcxInput.waitFor({ state: 'visible', timeout: 15000 });
    await mcxInput.fill('');
    await mcxInput.blur();

    // No PATCH for the empty/invalid entry — Number('') would be 0, which
    // must never be silently written as a real lead-time value.
    await page.waitForTimeout(500);
    expect(patches.length).toBe(0);
    await expect(mcxInput).toHaveValue('30');
  });
});
