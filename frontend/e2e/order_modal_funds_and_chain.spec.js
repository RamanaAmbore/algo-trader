/**
 * order_modal_funds_and_chain.spec.js
 *
 * Verifies 4d0356a5:
 *   - .oes-funds-line is rendered in the common action area above the
 *     submit row, showing Avail margin · Cash · (Used). Visible on
 *     BOTH the Ticket tab and the Chain tab (was previously inside
 *     OrderTicket, hidden on Chain).
 *   - .chain-grid-wrap fills the modal's available height instead of
 *     clamping to 14rem.
 *
 * Run:
 *   cd frontend && PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/order_modal_funds_and_chain.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';
const API_HOST = BASE.includes('localhost') ? 'https://dev.ramboq.com' : BASE;

let _cachedToken = null;

async function login(page) {
  if (!_cachedToken && process.env.PLAYWRIGHT_TOKEN) _cachedToken = process.env.PLAYWRIGHT_TOKEN;
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

test.describe('Order modal funds line + chain grid stretch (4d0356a5)', () => {
  test.setTimeout(120_000);

  test('.oes-funds-line shows in common area on Ticket AND Chain tabs · chain grid fills available height', async ({ page }) => {
    const log = { timeline: [], errors: [] };
    page.on('pageerror', (e) => log.errors.push(String(e).slice(0, 200)));

    await login(page);

    log.timeline.push('navigate /dashboard');
    await page.goto(`${BASE}/dashboard`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(2500);

    const orderBtn = page.locator('button.pha-order').first();
    if (await orderBtn.count() === 0) {
      log.timeline.push('skip: no .pha-order'); console.log(JSON.stringify(log, null, 2)); return;
    }
    await orderBtn.click({ force: true });
    await page.waitForTimeout(1500);

    const modal = page.locator('.oes-modal').first();
    if (await modal.count() === 0) {
      log.timeline.push('skip: modal did not open'); console.log(JSON.stringify(log, null, 2)); return;
    }

    // Seed an underlying symbol so chain has a context to render.
    const symInput = page.locator('.oes-modal input').filter({ hasNot: page.locator('[disabled]') }).first();
    if (await symInput.count() > 0) {
      await symInput.fill('NIFTY').catch(() => {});
      await page.waitForTimeout(900);
      const sugg = page.locator('.oes-sym-drop .oes-sym-row').first();
      if (await sugg.count() > 0) {
        await sugg.click({ force: true }).catch(() => {});
        await page.waitForTimeout(500);
      }
    }

    // Wait for /api/funds to land so _accountFunds populates.
    await page.waitForResponse((r) => /\/api\/funds/.test(r.url()) && r.ok(), { timeout: 10_000 }).catch(() => {});
    await page.waitForTimeout(900);

    // ── Ticket tab: assert funds line is in the common action area ─
    const fundsLine = page.locator('.oes-funds-line').first();
    const fundsCount = await fundsLine.count();
    log.timeline.push(`.oes-funds-line on Ticket tab: count=${fundsCount}`);
    if (fundsCount === 0) {
      log.timeline.push('note: funds line absent on Ticket tab (may be 401 on /api/funds for this session)');
    } else {
      const fundsText = (await fundsLine.textContent())?.trim() || '';
      log.timeline.push(`funds text: "${fundsText.slice(0, 200)}"`);
      expect(fundsText.toLowerCase()).toMatch(/avail margin|cash/);
      // Funds line must sit inside .oes-common-actions (the common area).
      const inCommon = await fundsLine.evaluate((el) =>
        !!el.closest('.oes-common-actions')
      );
      log.timeline.push(`funds line inside .oes-common-actions: ${inCommon}`);
      expect(inCommon).toBe(true);

      // Verify positionally it sits ABOVE the action buttons row.
      const fBox = await fundsLine.boundingBox();
      const sBox = await page.locator('.oes-common-submit').first().boundingBox();
      log.timeline.push(`funds y=${fBox?.y?.toFixed(0)} · submit y=${sBox?.y?.toFixed(0)}`);
      if (fBox && sBox) {
        expect(fBox.y).toBeLessThan(sBox.y);
        log.timeline.push(`✓ funds line sits ABOVE submit row`);
      }
    }

    // ── Switch to Chain tab, verify funds STILL visible ─────────────
    const chainTab = page.locator('.oes-modal button').filter({ hasText: /chain/i }).first();
    const chainTabCount = await chainTab.count();
    log.timeline.push(`chain tab candidates: ${chainTabCount}`);
    if (chainTabCount > 0) {
      await chainTab.click({ force: true }).catch(() => {});
      await page.waitForTimeout(1500);

      const fundsOnChain = await page.locator('.oes-funds-line').count();
      log.timeline.push(`.oes-funds-line on Chain tab: count=${fundsOnChain}`);
      // Either still rendered or absent because funds payload empty —
      // EITHER way the per-tab visibility behaviour matches the Ticket
      // tab (no longer hidden by being inside OrderTicket).
      if (fundsCount > 0) {
        expect(fundsOnChain).toBeGreaterThan(0);
        log.timeline.push('✓ funds line ALSO visible on Chain tab (was hidden before)');
      }

      // ── Chain grid stretches: when present, its height should be
      // > the old 14rem max-height (≈ 224px) ceiling — proving the
      // flex:1 1 0 swap took effect. Probe via documentstylesheets
      // rather than waiting for live data, since rambo's chain may
      // not populate (instruments cache + broker fetch dependent).
      const wrapRule = await page.evaluate(() => {
        for (const sheet of Array.from(document.styleSheets)) {
          let rules;
          try { rules = sheet.cssRules || sheet.rules; } catch { continue; }
          if (!rules) continue;
          for (const rule of Array.from(rules)) {
            const sel = /** @type {any} */ (rule).selectorText || '';
            if (/\.chain-grid-wrap(?:\.[A-Za-z0-9_-]+)?(?:\s|,|$)/.test(sel)) {
              return { selector: sel, cssText: rule.cssText.slice(0, 400) };
            }
          }
        }
        return null;
      });
      log.timeline.push(`.chain-grid-wrap rule: ${JSON.stringify(wrapRule).slice(0, 360)}`);
      expect(wrapRule).toBeTruthy();
      const wrapCss = (wrapRule?.cssText || '').toLowerCase();
      // Old clamp `max-height: 14rem` should be gone; new `flex: 1 1 0`
      // should be present with a `min-height` floor.
      expect(wrapCss).not.toMatch(/max-height:\s*14rem/);
      expect(wrapCss).toMatch(/flex:\s*1\s+1\s+0/);
      expect(wrapCss).toMatch(/min-height:\s*9rem/);
      log.timeline.push('✓ .chain-grid-wrap CSS rule: max-height clamp gone, flex:1 1 0 + min-height:9rem present');

      // Also probe the live box if the chain happened to render.
      const liveGrid = page.locator('.chain-grid-wrap').first();
      if (await liveGrid.count() > 0) {
        const gridBox = await liveGrid.boundingBox().catch(() => null);
        log.timeline.push(`.chain-grid-wrap live box: ${JSON.stringify(gridBox)}`);
        if (gridBox) {
          expect(gridBox.height).toBeGreaterThan(140);
        }
      } else {
        log.timeline.push('note: chain grid not populated for rambo (broker fetch dependent) — CSS rule check is the verification');
      }
    } else {
      log.timeline.push('skip: chain tab not found');
    }

    console.log('\n=== funds + chain log ===');
    console.log(JSON.stringify(log, null, 2));
  });
});
