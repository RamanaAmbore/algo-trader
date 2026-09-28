/**
 * order_ticket_mobile_overflow.spec.js
 *
 * Operator: "order ticket window is wider than viewport mobile
 * sometimes". The modal's own width was already correctly capped
 * (.ot-modal: 96vw, .oes-modal: 100% of its wrapper) on mobile, but
 * neither container clipped the X axis — an occasional wide child
 * (a long basket pill, a knobs row that hasn't wrapped yet, a symbol
 * badge) could still visually bleed past the modal edge and force
 * page-level horizontal scroll. Fix: `overflow-x: hidden` on both
 * .oes-modal (SymbolPanel.svelte) and .ot-modal (OrderTicket.svelte)
 * at the existing mobile breakpoints — a containment backstop, not a
 * replacement for each inner row's own overflow handling.
 *
 * This spec loads the heaviest ticket state reachable via the UI (a
 * real F&O option, picked through the Chain tab's CE Buy button) at a
 * 360px mobile viewport and asserts no element inside the modal, and
 * no document-level layout, exceeds the viewport width.
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *     npx playwright test e2e/order_ticket_mobile_overflow.spec.js \
 *     --project=chromium-desktop
 */
import { test, expect } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

let _cachedToken = null;
async function login(page) {
  if (!_cachedToken) {
    for (const u of ['rambo', 'ambore', 'admin']) {
      const r = await page.request.post(`${BASE}/api/auth/login`, {
        data: { username: u, password: _AUTH_PASS },
        timeout: 15_000,
      }).catch(() => null);
      if (r && r.ok()) { _cachedToken = (await r.json()).access_token; break; }
    }
    if (!_cachedToken) throw new Error('login failed');
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, _cachedToken);
}

test('order ticket + chain: no horizontal overflow at 360px mobile viewport', async ({ page }) => {
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 360, height: 800 });
  await login(page);
  await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2000);

  const orderBtn = page.locator('button.pha-order').first();
  if (await orderBtn.count() === 0) {
    test.skip(true, 'no .pha-order button found on /dashboard — skip');
    return;
  }
  await orderBtn.click({ force: true });
  await page.waitForTimeout(1200);

  const modal = page.locator('.oes-modal, .ot-modal').first();
  if (await modal.count() === 0) {
    test.skip(true, 'order modal did not open — skip');
    return;
  }

  // Load a real F&O option via underlying search → Chain tab → CE Buy,
  // the heaviest state the ticket/chain pair renders.
  try {
    const symInput = page.locator('.ssi-input').first();
    if (await symInput.count() > 0) {
      await symInput.click();
      await symInput.fill('NIFTY');
      await page.waitForTimeout(1000);
      const rows = page.locator('.ssi-row');
      if (await rows.count() > 0) await rows.first().click();
      await page.waitForTimeout(1000);

      const chainTab = page.getByRole('tab', { name: /^Chain/i }).first();
      if (await chainTab.count() > 0 && !(await chainTab.isDisabled().catch(() => true))) {
        await chainTab.click();
        await page.waitForTimeout(1500);
        const ceBuy = page.locator('.chain-btn-buy').first();
        if (await ceBuy.count() > 0) {
          await ceBuy.click();
          await page.waitForTimeout(1000);
        }
      }
    }
  } catch {
    // Best-effort — even the empty/base ticket state must not overflow.
  }

  const report = await page.evaluate(() => {
    const vw = window.innerWidth;
    const offenders = [];
    document
      .querySelectorAll('.oes-modal, .oes-modal *, .ot-modal, .ot-modal *')
      .forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.right > vw + 2 || r.width > vw + 2) {
          offenders.push({
            tag: el.tagName,
            cls: (el.className?.toString?.() || '').slice(0, 80),
            right: Math.round(r.right), width: Math.round(r.width),
          });
        }
      });
    return {
      docScrollWidth: document.documentElement.scrollWidth,
      vw,
      offenders,
    };
  });

  expect(
    report.offenders,
    `elements overflowing the 360px viewport inside the order modal: ${JSON.stringify(report.offenders)}`
  ).toEqual([]);
  expect(
    report.docScrollWidth,
    `document.documentElement.scrollWidth (${report.docScrollWidth}) exceeds viewport width (${report.vw}) — page-level horizontal scroll`
  ).toBeLessThanOrEqual(report.vw + 2);
});
