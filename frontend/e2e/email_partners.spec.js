/**
 * Email Partners panel — /admin regression spec.
 *
 * Defect (operator: 2026-07-03): "email from partner from users page is not
 * working". Root cause: the panel's preset dropdown sent underscore-cased
 * preset values (`all_partners`, `all_designated`, `all_users`) to the
 * backend, while `POST /api/admin/email-partners` accepts only the
 * hyphen-cased canonical vocabulary (`all-partners`, `all-designated`,
 * `all`). Preset send always got a 422 with a "recipients must be a list
 * of usernames or one of…" detail; operator saw the failure toast.
 *
 * Fix in `frontend/src/routes/(algo)/admin/+page.svelte`:
 *   - PRESET_OPTIONS values realigned to backend vocabulary
 *   - emailRecipientLabel + button-label conditionals updated to match
 *
 * This spec pins the frontend contract via the outgoing request body.
 * We do NOT hit real SMTP — the `/api/admin/email-partners` route is
 * intercepted via `page.route()` and a canned success payload is
 * returned. What matters here is the request the frontend sent.
 *
 * Five quality dimensions:
 *   SSOT     — request.recipients string MUST be one of the three
 *              canonical hyphen presets or an array of usernames.
 *   Perf     — a single POST fires per Send click (no fan-out / retry).
 *   Stale    — assert legacy underscore variants are NEVER emitted.
 *   Reusable — the same `sendPartnerEmail` helper carries the payload;
 *              its contract is the SSOT the spec pins.
 *   UX       — success toast renders with the sent/total count from the
 *              server response; button re-enables after send.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test email_partners.spec.js \
 *   --workers=1 --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';

/** Canonical preset vocabulary — MUST match backend
 *  `EmailPartnersRequest` docstring at admin.py:183.  */
const CANONICAL_PRESETS = new Set(['all-partners', 'all-designated', 'all']);

/** Legacy pre-fix values — MUST NEVER re-appear in requests. */
const LEGACY_UNDERSCORE_PRESETS = new Set(['all_partners', 'all_designated', 'all_users']);


test.describe('Email Partners — preset vocabulary contract', () => {

  test('preset "All partners" → dispatches hyphen preset (all-partners)',
       async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/admin`, { waitUntil: 'domcontentloaded' });

    // Wait for the panel to render — it's below the users table and
    // only mounts when `users.length > 0`.
    const panel = page.locator('section.email-panel').first();
    await panel.waitFor({ state: 'visible', timeout: 15000 });

    // Intercept the POST so no real email fires + we can inspect the
    // outgoing body.
    let capturedBody = /** @type {any} */ (null);
    let requestCount = 0;
    await page.route('**/api/admin/email-partners', async (route) => {
      requestCount += 1;
      capturedBody = route.request().postDataJSON();
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({
          sent_count:   3,
          failed_count: 0,
          total:        3,
          event_id:     123,
          failures:     [],
        }),
      });
    });

    // Also intercept the email-events poll so it doesn't spam the log
    // panel — trivial no-op response.
    await page.route('**/api/admin/email-events**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json',
                      body: JSON.stringify({ events: [] }) }));

    // Pick the "All partners" preset — click the Select trigger, then
    // the matching <li role="option">.
    await panel.getByLabel('Recipient preset').click();
    // Options render as <li role="option"> with the label as visible
    // text. Match "All partners" strictly — "All designated" contains
    // the same substring seed.
    await page.getByRole('option', { name: /^All partners$/ }).click();

    // Fill subject + body.
    await panel.locator('input[placeholder="Email subject…"]')
                .fill('E2E preset test');
    await panel.locator('textarea[placeholder="Message body…"]')
                .fill('Regression pin for the underscore-preset defect.');

    // Click Send — the button carries "Send to N partner(s)" text.
    await panel.getByRole('button', { name: /^Send to/ }).click();

    // ConfirmModal opens — click the Send button in its footer.
    // The confirm dialog has role="dialog" + aria-label matches title.
    const dlg = page.getByRole('dialog', { name: /Send email\?/ });
    await dlg.waitFor({ state: 'visible', timeout: 5000 });
    await dlg.getByRole('button', { name: /^Send$/ }).click();

    // Poll for the intercepted request — up to 5 s.
    await expect.poll(() => requestCount, { timeout: 5000 }).toBe(1);

    // Contract assertions ---------------------------------------------
    expect(capturedBody).not.toBeNull();
    expect(capturedBody.subject).toBe('E2E preset test');
    expect(capturedBody.body).toContain('underscore-preset');

    // Preset MUST be canonical hyphen form.
    expect(typeof capturedBody.recipients).toBe('string');
    expect(CANONICAL_PRESETS.has(capturedBody.recipients)).toBe(true);
    expect(capturedBody.recipients).toBe('all-partners');

    // Stale-code guard — never a legacy underscore.
    expect(LEGACY_UNDERSCORE_PRESETS.has(capturedBody.recipients)).toBe(false);

    // Success toast surfaces the server's sent/total counts.
    const toast = panel.locator('div', {
      hasText: /Sent to 3\/3/
    }).first();
    await toast.waitFor({ state: 'visible', timeout: 5000 });
  });


  test('preset "All designated" → dispatches all-designated',
       async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/admin`, { waitUntil: 'domcontentloaded' });

    const panel = page.locator('section.email-panel').first();
    await panel.waitFor({ state: 'visible', timeout: 15000 });

    let capturedBody = /** @type {any} */ (null);
    await page.route('**/api/admin/email-partners', async (route) => {
      capturedBody = route.request().postDataJSON();
      await route.fulfill({
        status: 201, contentType: 'application/json',
        body: JSON.stringify({ sent_count: 1, failed_count: 0, total: 1,
                                event_id: 124, failures: [] }),
      });
    });
    await page.route('**/api/admin/email-events**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json',
                      body: JSON.stringify({ events: [] }) }));

    await panel.getByLabel('Recipient preset').click();
    await page.getByRole('option', { name: /^All designated$/ }).click();
    await panel.locator('input[placeholder="Email subject…"]').fill('X');
    await panel.locator('textarea[placeholder="Message body…"]').fill('X');
    await panel.getByRole('button', { name: /^Send to/ }).click();
    const dlg = page.getByRole('dialog', { name: /Send email\?/ });
    await dlg.waitFor({ state: 'visible', timeout: 5000 });
    await dlg.getByRole('button', { name: /^Send$/ }).click();

    await expect.poll(() => capturedBody?.recipients,
                       { timeout: 5000 }).toBe('all-designated');
    expect(LEGACY_UNDERSCORE_PRESETS.has(capturedBody.recipients)).toBe(false);
  });


  test('preset "All users" → dispatches all',
       async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/admin`, { waitUntil: 'domcontentloaded' });

    const panel = page.locator('section.email-panel').first();
    await panel.waitFor({ state: 'visible', timeout: 15000 });

    let capturedBody = /** @type {any} */ (null);
    await page.route('**/api/admin/email-partners', async (route) => {
      capturedBody = route.request().postDataJSON();
      await route.fulfill({
        status: 201, contentType: 'application/json',
        body: JSON.stringify({ sent_count: 5, failed_count: 0, total: 5,
                                event_id: 125, failures: [] }),
      });
    });
    await page.route('**/api/admin/email-events**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json',
                      body: JSON.stringify({ events: [] }) }));

    await panel.getByLabel('Recipient preset').click();
    await page.getByRole('option', { name: /^All users$/ }).click();
    await panel.locator('input[placeholder="Email subject…"]').fill('X');
    await panel.locator('textarea[placeholder="Message body…"]').fill('X');
    await panel.getByRole('button', { name: /^Send to/ }).click();
    const dlg = page.getByRole('dialog', { name: /Send email\?/ });
    await dlg.waitFor({ state: 'visible', timeout: 5000 });
    await dlg.getByRole('button', { name: /^Send$/ }).click();

    // "All users" preset in the UI maps to backend's `all` preset —
    // not `all_users` (defect) and not `all-users` (never existed).
    await expect.poll(() => capturedBody?.recipients,
                       { timeout: 5000 }).toBe('all');
    expect(LEGACY_UNDERSCORE_PRESETS.has(capturedBody.recipients)).toBe(false);
  });


  test('button re-enables after Send resolves',
       async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/admin`, { waitUntil: 'domcontentloaded' });

    const panel = page.locator('section.email-panel').first();
    await panel.waitFor({ state: 'visible', timeout: 15000 });

    await page.route('**/api/admin/email-partners', async (route) => {
      await route.fulfill({
        status: 201, contentType: 'application/json',
        body: JSON.stringify({ sent_count: 1, failed_count: 0, total: 1,
                                event_id: 126, failures: [] }),
      });
    });
    await page.route('**/api/admin/email-events**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json',
                      body: JSON.stringify({ events: [] }) }));

    await panel.getByLabel('Recipient preset').click();
    await page.getByRole('option', { name: /^All partners$/ }).click();
    await panel.locator('input[placeholder="Email subject…"]').fill('X');
    await panel.locator('textarea[placeholder="Message body…"]').fill('X');

    const sendBtn = panel.getByRole('button', { name: /^Send to/ });
    await sendBtn.click();
    const dlg = page.getByRole('dialog', { name: /Send email\?/ });
    await dlg.waitFor({ state: 'visible', timeout: 5000 });
    await dlg.getByRole('button', { name: /^Send$/ }).click();

    // Post-send, the button re-enables within 10 s — no perpetual
    // "Sending…" spinner. The disabled attribute clears when
    // `sending=false` runs in the finally block.
    await expect(sendBtn).toBeEnabled({ timeout: 10000 });
  });
});
