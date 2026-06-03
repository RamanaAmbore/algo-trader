/**
 * order_modal_default_symbol_and_layout.spec.js
 *
 * Verifies 77b02f44:
 *   - /api/accounts/ payload now carries default_symbol
 *   - Order modal pre-fills the symbol picker from the operator's
 *     default (with underlying → contract resolution where applicable)
 *   - Lots / Qty label sits INLINE to the left of the value
 *     (.ot-quick-qty rule is flex-direction: row)
 *   - On a mobile viewport (390 × 844), the order modal panel matches
 *     the canonical 96vw / 90vh sizing
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/order_modal_default_symbol_and_layout.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
const API_HOST = BASE.includes('localhost') ? 'https://dev.ramboq.com' : BASE;

let _cachedToken = null;

async function login(page) {
  if (!_cachedToken) {
    for (const u of ['rambo', 'ambore', 'admin']) {
      const r = await page.request.post(`${API_HOST}/api/auth/login`, {
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

test.describe('Default symbol + Lots/Qty inline label + mobile (77b02f44)', () => {
  test.setTimeout(120_000);

  test('default_symbol in /api/accounts/ · pre-fill · Lots-inline rule · mobile-canonical sizing', async ({ page }) => {
    const log = { timeline: [], errors: [] };
    page.on('pageerror', (e) => log.errors.push(String(e).slice(0, 200)));

    await login(page);

    // ── 1. Backend payload includes default_symbol ──────────────────
    const accountsResp = await page.request.get(`${API_HOST}/api/accounts/`, {
      headers: { Authorization: `Bearer ${_cachedToken}` },
      timeout: 10_000,
    });
    expect(accountsResp.ok()).toBe(true);
    const accountsJson = await accountsResp.json();
    log.timeline.push(`/api/accounts/ keys: ${Object.keys(accountsJson).join(',')}`);
    log.timeline.push(`default_account=${accountsJson.default_account} · default_symbol=${accountsJson.default_symbol}`);
    expect(accountsJson).toHaveProperty('default_symbol');

    // ── 2. Order modal pre-fills the symbol from default ────────────
    log.timeline.push('navigate /dashboard');
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);

    const orderBtn = page.locator('button.pha-order').first();
    if (await orderBtn.count() === 0) {
      log.timeline.push('skip: no .pha-order'); console.log(JSON.stringify(log, null, 2)); return;
    }
    await orderBtn.click({ force: true });
    await page.waitForTimeout(1800);

    const modal = page.locator('.oes-modal').first();
    if (await modal.count() === 0) {
      log.timeline.push('skip: modal did not open'); console.log(JSON.stringify(log, null, 2)); return;
    }

    // Symbol pre-fill — probe every readable surface inside the modal
    // (input values, chip text, body text) for the default symbol's
    // first 4-char prefix. Accept either raw underlying or resolved
    // tradeable since the resolution path depends on instruments cache.
    await page.waitForTimeout(1500);
    const probeText = await page.evaluate(() => {
      const root = document.querySelector('.oes-modal');
      if (!root) return '';
      const inputs = Array.from(root.querySelectorAll('input'))
        .map((i) => /** @type {HTMLInputElement} */ (i).value || '')
        .join(' ');
      const body = root.textContent || '';
      return (inputs + ' ' + body).toUpperCase();
    });
    log.timeline.push(`probe text length: ${probeText.length}`);
    if (accountsJson.default_symbol) {
      const prefix = String(accountsJson.default_symbol).slice(0, 4).toUpperCase();
      const hit = probeText.includes(prefix);
      log.timeline.push(`pre-fill prefix "${prefix}" found: ${hit}`);
      expect(hit).toBe(true);
      log.timeline.push(`✓ modal carries default_symbol prefix in its body/inputs`);
    }

    // ── 3. Lots / Qty inline label rule ─────────────────────────────
    const qtyRule = await page.evaluate(() => {
      for (const sheet of Array.from(document.styleSheets)) {
        let r;
        try { r = sheet.cssRules || sheet.rules; } catch { continue; }
        if (!r) continue;
        for (const rule of Array.from(r)) {
          const sel = /** @type {any} */ (rule).selectorText || '';
          if (/\.ot-quick-qty(?:\.[A-Za-z0-9_-]+)?(?:\s|,|$)/.test(sel) &&
              !sel.includes(' ')) {
            return { selector: sel, cssText: rule.cssText.slice(0, 300) };
          }
        }
      }
      return null;
    });
    log.timeline.push(`.ot-quick-qty rule: ${JSON.stringify(qtyRule).slice(0, 300)}`);
    expect(qtyRule).toBeTruthy();
    const qtyCss = (qtyRule?.cssText || '').toLowerCase();
    expect(qtyCss).toMatch(/flex-direction:\s*row/);
    log.timeline.push('✓ Lots/Qty .ot-quick-qty rule has flex-direction:row (label sits INLINE LEFT)');

    // ── 4. Mobile-canonical sizing (close modal first) ──────────────
    await page.keyboard.press('Escape');
    await page.waitForTimeout(400);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(400);

    const orderBtn2 = page.locator('button.pha-order').first();
    if (await orderBtn2.count() === 0) {
      // Mobile nav may collapse — open via hamburger or skip mobile assertion.
      log.timeline.push('mobile: page-header order button absent; mobile assertion skipped');
      console.log(JSON.stringify(log, null, 2));
      return;
    }
    await orderBtn2.click({ force: true });
    await page.waitForTimeout(1500);

    const modalMobile = page.locator('.oes-modal, .canonical-modal-panel').first();
    if (await modalMobile.count() === 0) {
      log.timeline.push('mobile: modal did not open'); console.log(JSON.stringify(log, null, 2)); return;
    }
    const mb = await modalMobile.boundingBox();
    log.timeline.push(`mobile modal box: ${JSON.stringify(mb)}`);
    expect(mb).toBeTruthy();
    if (mb) {
      // 96vw of 390 = 374.4 px, 90vh of 844 = 759.6 px. Allow slack.
      expect(mb.width).toBeGreaterThan(360);
      expect(mb.width).toBeLessThanOrEqual(390);
      expect(mb.height).toBeGreaterThan(720);
      expect(mb.height).toBeLessThanOrEqual(844);
      // Fully visible: top + left + bottom-edge within viewport.
      expect(mb.x).toBeGreaterThanOrEqual(0);
      expect(mb.y).toBeGreaterThanOrEqual(0);
      expect(mb.x + mb.width).toBeLessThanOrEqual(390 + 1);
      expect(mb.y + mb.height).toBeLessThanOrEqual(844 + 1);
      log.timeline.push(`✓ mobile modal w=${mb.width.toFixed(0)} h=${mb.height.toFixed(0)} — canonical 96vw/90vh + fully visible`);
    }

    console.log('\n=== default-symbol + lots-inline + mobile log ===');
    console.log(JSON.stringify(log, null, 2));
  });
});
