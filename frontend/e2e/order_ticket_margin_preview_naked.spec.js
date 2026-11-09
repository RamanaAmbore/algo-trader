/**
 * order_ticket_margin_preview_naked.spec.js
 *
 * 2026-10-20 fix (plan item 8, + dissolves item 9.2) — OrderTicket's
 * margin-preview `$effect` (the one calling `previewOrderMargin` /
 * `POST /api/orders/preflight`) used to send `paired_legs` built from
 * the template-preview's resolved wing (`_previewPlan?.wing`), so the
 * backend's `run_preflight` computed NET spread margin (parent netted
 * against the protective wing). The REAL submit-time preflight check
 * (`_ticket_run_preflight` in backend/api/routes/orders_place.py) calls
 * `run_preflight` with NO paired orders at all — the wing is only ever
 * placed on PARENT FILL via template attach, so at the moment of
 * submission the broker genuinely only sees the naked parent order and
 * needs naked margin for it. The preview could show "sufficient
 * margin" for what looked like a hedged position while the real submit
 * a moment later enforced the (larger) naked requirement — a display
 * bug, not a looser real gate. Fix: the margin-preview request no
 * longer sends `paired_legs` at all, so it is naked by construction —
 * matching what submit actually enforces.
 *
 * This also dissolves the separate item 9.2 finding (the untracked
 * `_previewPlan?.wing` read inside the margin effect's debounced
 * `setTimeout`, which could race a stale wing from a previous
 * strike/side into the margin number): once the margin request stops
 * reading the wing at all, that read — and its race — no longer
 * exists. Nothing further to fix for 9.2 once 8 is in.
 *
 * 2026-11 (Track 3, T3.4, Bracket/Template/GTT removal) — the old
 * second test here drove the TemplateBar "Bracket active" toggle
 * (`.oes-tpl-button`) to prove `paired_legs` stayed absent even with a
 * template wing resolved. TemplateBar.svelte is deleted and there is no
 * surviving way to arm a bracket/wing on OrderTicket at all, so that
 * live UI flow is gone — removed rather than rewritten, since there is
 * no bracket behavior left to drive. The source-level test below still
 * covers the real invariant: the margin-preview payload never builds
 * `paired_legs`, full stop (not merely "not from the wing").
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import { readFileSync } from 'fs';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const ROOT = '/Users/ramanambore/projects/ramboq/frontend/src/lib';

async function mockPaperMode(page) {
  await page.route('**/api/admin/execution/mode', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          mode: 'paper',
          branch: 'dev',
          allowed_modes: ['idle', 'sim', 'replay', 'paper', 'shadow'],
        }),
      });
    } else {
      await route.continue();
    }
  });
}

async function _seedNiftyOnTicket(page) {
  await page.goto(`${BASE}/orders`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const symInput = page.locator('.ssi-input').first();
  await expect(symInput).toBeVisible({ timeout: 15_000 });
  await symInput.fill('NIFTY');
  const sugg = page.locator('.ssi-drop .ssi-row').first();
  await expect(sugg).toBeVisible({ timeout: 10_000 });
  await sugg.click({ force: true });
  await page.waitForTimeout(500);
}

test.describe('Order Ticket — margin preview is naked, not netted with the wing', () => {
  test('source: previewOrderMargin payload no longer builds paired_legs from the template wing', () => {
    const src = readFileSync(`${ROOT}/order/OrderTicket.svelte`, 'utf-8');
    const payloadStart = src.indexOf('const payload = {');
    const callIdx = src.indexOf('await previewOrderMargin(payload)');
    expect(payloadStart, 'payload object literal not found').toBeGreaterThan(-1);
    expect(callIdx, 'previewOrderMargin call not found').toBeGreaterThan(payloadStart);
    // Scope strictly to the payload object literal itself (not the
    // explanatory comment above it, which legitimately mentions the
    // removed field by name).
    const block = src.slice(payloadStart, src.indexOf('};', payloadStart));
    expect(block).not.toMatch(/paired_legs/);
    expect(block).not.toMatch(/_previewPlan\?\.wing/);
  });

  test('live: a real /api/orders/preflight request never carries paired_legs', async ({ page }) => {
    test.setTimeout(60_000);
    let preflightBody = null;
    let sawPreflightCall = false;
    await page.route('**/api/orders/preflight', async (route) => {
      sawPreflightCall = true;
      preflightBody = route.request().postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ok: true,
          blocked: [],
          diagnostics: { basket_margin_used: 12345, available_margin: 999999, margin_shortfall: 0 },
        }),
      });
    });
    await mockPaperMode(page);
    await loginAsAdmin(page);
    await _seedNiftyOnTicket(page);

    const buyPill = page.locator('button.ot-side-buy').first();
    if (await buyPill.count() > 0 && await buyPill.isEnabled().catch(() => false)) {
      await buyPill.click();
    }

    // Give the 350ms margin-preview debounce time to fire.
    await page.waitForTimeout(1000);

    expect(sawPreflightCall, 'expected at least one /api/orders/preflight request').toBe(true);
    expect(preflightBody, 'preflight request body must have been captured').not.toBeNull();
    expect(preflightBody.paired_legs, 'margin preview must be naked — paired_legs must not be sent').toBeUndefined();
  });
});
